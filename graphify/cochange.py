"""Optional co-change enrichment via the standalone `graphmine` tool.

graphmine (https://github.com/PaavoReinikka/graphmine) mines statistically
significant *co-change* couplings from a repo's git history and can emit them as
additive, typed ``co_changes_with`` edges over a graphify ``graph.json``. graphify
shells out to the ``graphmine`` CLI when it is installed — there is **no hard
dependency**, and graphmine's Kingfisher/Rust stack stays entirely out of
graphify. If ``graphmine`` is not on PATH this is a friendly no-op.

The result is additive: graphmine writes ``<out>/cochange.graphify.json`` (a copy
of the graph with the extra edges) plus a standalone report — the original
``graph.json`` is left untouched, so the temporal-coupling layer stays optional
and filterable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Marker-delimited section appended to assistant instruction files (CLAUDE.md,
# AGENTS.md, ...) by `graphify cochange --update-instructions`. Its own `## `
# heading so graphify's _replace_or_append_section treats it as a sibling of the
# base `## graphify` block (idempotent update, removable, never rewrites the base
# block). Kept inline (not an always_on/ file) so it stays out of skillgen's
# always-on-roundtrip validation — this is a fork-local addition.
_COCHANGE_MARKER = "## graphify: co-change"
_COCHANGE_INSTRUCTION = """\
## graphify: co-change

This project also has a statistical **co-change** layer mined from git history
(via graphmine): `graphify-out/cochange.md` is a clustered digest and
`graphify-out/cochange.graphify.json` adds `co_changes_with` edges (a STATISTICAL
confidence tier whose score is an FDR-corrected q-value; smaller = stronger).

Rules:
- For impact / blast-radius / refactoring questions, consult the co-change layer:
  files that change together are coupled in ways static structure (imports,
  calls) cannot show — e.g. generated-together files, a config and its consumer,
  a SQL table and its triggers.
- When editing or assessing a file, check its co-change partners in
  `graphify-out/cochange.md` and weigh them in "what else might need to change".
- These edges are statistical hints, not guarantees — weight them by the q-value.
"""

# Instruction files that carry graphify's `## graphify` always-on block, relative
# to the project root. We only touch ones that already opted into graphify.
_INSTRUCTION_CANDIDATES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "CODEBUDDY.md",
    ".github/copilot-instructions.md",
)
_GRAPHIFY_BLOCK_MARKER = "## graphify"


def graphmine_available() -> bool:
    return shutil.which("graphmine") is not None


def enrich_with_cochange(
    repo: Path,
    graph_path: Path,
    out_dir: Path,
    *,
    correction: str = "bh",
    alpha: float = 0.05,
    subsystem_depth: int = 1,
    include_deleted: bool = False,
    extra_args: list[str] | None = None,
) -> int | None:
    """Run ``graphmine cochange`` to augment *graph_path*. Returns the subprocess
    exit code, or ``None`` if graphmine is not installed."""
    exe = shutil.which("graphmine")
    if not exe:
        print(
            "[graphify] co-change enrichment needs the 'graphmine' tool, which is "
            "not on PATH.\n           Install it from "
            "https://github.com/PaavoReinikka/graphmine and re-run.",
            file=sys.stderr,
        )
        return None

    cmd = [
        exe, "cochange", str(repo),
        "--graphify-graph", str(graph_path),
        "--out", str(out_dir),
        "--correction", correction,
        "--alpha", str(alpha),
        "--subsystem-depth", str(subsystem_depth),
    ]
    if include_deleted:
        cmd.append("--include-deleted")
    if extra_args:
        cmd += extra_args
    # encoding="utf-8": graphmine's digest contains non-ASCII (⇔, ·) that would
    # crash a cp1252 pipe on Windows.
    proc = subprocess.run(cmd, encoding="utf-8")
    if proc.returncode == 0:
        print(f"[graphify] co-change layer written under {out_dir} "
              f"(cochange.md + cochange.graphify.json — graph.json left untouched).")
    return proc.returncode


def update_instructions(project_dir: Path) -> list[Path]:
    """Add the co-change awareness section to assistant instruction files that
    already carry graphify's `## graphify` block. Idempotent; returns the files
    updated. Only touches files that opted into graphify (have the base block),
    so it never creates instructions out of nowhere."""
    from graphify.__main__ import _replace_or_append_section

    updated: list[Path] = []
    for rel in _INSTRUCTION_CANDIDATES:
        path = project_dir / rel
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if _GRAPHIFY_BLOCK_MARKER not in content:
            continue  # file isn't graphify-configured; leave it alone
        new_content = _replace_or_append_section(
            content, _COCHANGE_MARKER, _COCHANGE_INSTRUCTION
        )
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            updated.append(path)
    if updated:
        print("[graphify] co-change guidance added to: "
              + ", ".join(str(p) for p in updated))
    else:
        print("[graphify] no graphify-configured instruction files found to update "
              "(run `graphify <platform> install` first).", file=sys.stderr)
    return updated
