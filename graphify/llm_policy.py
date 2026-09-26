"""Opt-in LLM backend selection (fork addition).

Upstream graphify picks an LLM on its own: the first API key found in the
environment (gemini -> kimi -> claude -> openai -> ...), and for community
naming and PR triage it silently falls back to the local ``claude`` CLI. That
sends code-derived text to a provider nobody chose. In this fork an LLM is
used only when one was selected explicitly, and every call is announced.

Selection, highest precedence first:

1. ``--backend <name>`` on the command (where the command accepts it);
2. ``GRAPHIFY_BACKEND=<name>`` in the environment — set it once in your shell
   profile, e.g. ``GRAPHIFY_BACKEND=claude-cli``;
3. ``GRAPHIFY_BACKEND=auto`` restores upstream's auto-detection at every call
   site, exactly as upstream does it there (announced where the backend is
   known). The fork's test-suite runs upstream's tests in this mode.

Unset means *no LLM*: each operation takes its offline path (community names
from their hub node, docs skipped) and says how to enable one.

Model: ``--model`` where accepted, else the backend's own setting. For
``claude-cli`` that is ``GRAPHIFY_CLAUDE_CLI_MODEL`` (upstream's variable),
which this fork defaults to ``sonnet`` — without it ``claude -p`` runs on your
Claude Code default model, often Opus, which is overkill for graphify's
structured extraction and naming. Set ``GRAPHIFY_CLAUDE_CLI_MODEL=opus`` to
use Opus.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

ENV_BACKEND = "GRAPHIFY_BACKEND"
ENV_CLAUDE_CLI_MODEL = "GRAPHIFY_CLAUDE_CLI_MODEL"
DEFAULT_CLAUDE_CLI_MODEL = "sonnet"
AUTO = "auto"
_NONE_VALUES = ("", "none", "off", "0", "false")

__all__ = [
    "Choice", "resolve", "announce", "not_selected_message", "available_backends",
    "is_auto", "detect_or_claude_cli",
    "ENV_BACKEND", "ENV_CLAUDE_CLI_MODEL", "DEFAULT_CLAUDE_CLI_MODEL",
]


@dataclass(frozen=True)
class Choice:
    backend: str
    model: str | None  # None = the backend's own default
    source: str        # "--backend", "GRAPHIFY_BACKEND" or "GRAPHIFY_BACKEND=auto"

    @property
    def description(self) -> str:
        if self.backend == "claude-cli":
            return "claude-cli (Claude Code, your subscription)"
        return self.backend


def _claude_cli_on_path() -> bool:
    from graphify.llm import _claude_cli_available
    return _claude_cli_available()


def available_backends() -> list[str]:
    """Backends usable right now: credentials present, or `claude` on PATH."""
    from graphify.llm import BACKENDS, _get_backend_api_key, _resolve_ollama_base_url
    found: list[str] = []
    if _claude_cli_on_path():
        found.append("claude-cli")
    for name in BACKENDS:
        if name == "claude-cli":
            continue
        try:
            if name == "ollama":
                ok = bool(_resolve_ollama_base_url(""))
            elif name == "bedrock":
                ok = any(os.environ.get(k) for k in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"))
            elif name == "azure":
                ok = bool(_get_backend_api_key("azure") and os.environ.get("AZURE_OPENAI_ENDPOINT"))
            else:
                ok = bool(_get_backend_api_key(name))
        except Exception:
            ok = False
        if ok:
            found.append(name)
    return found


def is_auto() -> bool:
    return os.environ.get(ENV_BACKEND, "").strip().lower() == AUTO


def detect_or_claude_cli() -> str | None:
    """Upstream's detection for community naming: a key, else the claude CLI."""
    from graphify.llm import detect_backend
    backend = detect_backend()
    if backend is None and _claude_cli_on_path():
        backend = "claude-cli"
    return backend


def resolve(explicit: str | None = None, model: str | None = None, *,
            auto=None) -> Choice | None:
    """The selected backend (and model), or None for no LLM.

    *auto* is the call site's upstream detection, used only under
    ``GRAPHIFY_BACKEND=auto`` (default: ``detect_backend``). Raises ValueError
    for an unknown backend name so a typo fails loudly instead of silently
    disabling the LLM.
    """
    from graphify.llm import BACKENDS
    source = "--backend"
    backend = (explicit or "").strip() or None
    if backend is None:
        env = os.environ.get(ENV_BACKEND, "").strip()
        if env.lower() in _NONE_VALUES:
            return None
        if env.lower() == AUTO:
            if auto is None:
                from graphify.llm import detect_backend as auto
            backend, source = auto(), f"{ENV_BACKEND}={AUTO}"
            if backend is None:
                return None
        else:
            backend, source = env, ENV_BACKEND
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown LLM backend {backend!r} (from {source}). "
            f"Available: {', '.join(sorted(BACKENDS))}"
        )
    if backend == "claude-cli":
        model = (model or os.environ.get(ENV_CLAUDE_CLI_MODEL, "").strip()
                 or DEFAULT_CLAUDE_CLI_MODEL)
        # Upstream's claude-cli extraction path reads this variable directly;
        # setting it (process-local) makes every claude-cli call use one model.
        os.environ[ENV_CLAUDE_CLI_MODEL] = model
    return Choice(backend=backend, model=model or None, source=source)


def announce(choice: Choice, purpose: str, *, stream=None) -> str:
    """Print (to stderr by default) and return the one-line LLM-use notice."""
    model = choice.model or "backend default"
    line = (f"[graphify] {purpose}: calling LLM via {choice.description}, "
            f"model {model} (selected by {choice.source}).")
    print(line, file=stream or sys.stderr, flush=True)
    return line


def not_selected_message(purpose: str, fallback: str) -> str:
    """Explain that no LLM is selected, what happens instead, and the options."""
    avail = available_backends()
    lines = [f"[graphify] {purpose} can use an LLM, but none is selected; {fallback}."]
    if avail:
        lines.append(f"           Available here: {', '.join(avail)}.")
    else:
        lines.append("           No LLM found here (no API key, no `claude` CLI on PATH).")
    lines.append(f"           Choose one with --backend=<name> or set {ENV_BACKEND} "
                 f"(e.g. {ENV_BACKEND}=claude-cli); {ENV_BACKEND}={AUTO} restores auto-detect.")
    return "\n".join(lines)
