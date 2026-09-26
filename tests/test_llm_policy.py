"""Fork: LLM use is opt-in and announced (graphify/llm_policy.py).

conftest.py runs upstream's tests with GRAPHIFY_BACKEND=auto; every test here
sets or unsets it explicitly.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from graphify import llm_policy


@pytest.fixture
def no_backend(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_BACKEND", raising=False)
    monkeypatch.delenv("GRAPHIFY_CLAUDE_CLI_MODEL", raising=False)
    monkeypatch.setattr(llm_policy, "_claude_cli_on_path", lambda: True)


# ── resolution ───────────────────────────────────────────────────────────────

def test_unset_means_no_llm_even_with_keys_and_claude_cli(no_backend, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm_policy.resolve() is None


def test_env_selects_claude_cli_with_sonnet_default(no_backend, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    choice = llm_policy.resolve()
    assert (choice.backend, choice.model, choice.source) == ("claude-cli", "sonnet", "GRAPHIFY_BACKEND")
    # the upstream extraction path reads this variable directly
    import os
    assert os.environ["GRAPHIFY_CLAUDE_CLI_MODEL"] == "sonnet"


def test_claude_cli_model_from_env_or_flag(no_backend, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    monkeypatch.setenv("GRAPHIFY_CLAUDE_CLI_MODEL", "opus")
    assert llm_policy.resolve().model == "opus"
    assert llm_policy.resolve(model="haiku").model == "haiku"


def test_flag_beats_env_and_other_backends_keep_their_default_model(no_backend, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    choice = llm_policy.resolve("ollama")
    assert (choice.backend, choice.model, choice.source) == ("ollama", None, "--backend")


def test_unknown_backend_fails_loudly(no_backend, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-clii")
    with pytest.raises(ValueError, match="claude-clii"):
        llm_policy.resolve()


def test_auto_delegates_to_the_call_sites_detection(no_backend, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "auto")
    assert llm_policy.resolve(auto=lambda: None) is None
    choice = llm_policy.resolve(auto=lambda: "azure")
    assert (choice.backend, choice.source) == ("azure", "GRAPHIFY_BACKEND=auto")


@pytest.mark.parametrize("value", ["none", "off", ""])
def test_explicit_off_values(no_backend, monkeypatch, value):
    monkeypatch.setenv("GRAPHIFY_BACKEND", value)
    assert llm_policy.resolve() is None


def test_announce_and_not_selected_messages(no_backend, monkeypatch, capsys):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    line = llm_policy.announce(llm_policy.resolve(), "community naming (3 communities)")
    assert line in capsys.readouterr().err
    assert "claude-cli (Claude Code, your subscription)" in line and "model sonnet" in line
    msg = llm_policy.not_selected_message("PR triage", "skipping the ranking")
    assert "none is selected; skipping the ranking" in msg
    assert "claude-cli" in msg and "GRAPHIFY_BACKEND=claude-cli" in msg


# ── call sites ───────────────────────────────────────────────────────────────

def _graph_dir(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = {"directed": False, "multigraph": False,
             "nodes": [{"id": "n1", "label": "OrderService", "community": 0}], "links": []}
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return tmp_path


def _run_label(tmp_path, monkeypatch, *extra):
    import graphify.__main__ as cli
    calls = []

    def fake_generate(G, communities, *, backend=None, model=None, **_kw):
        calls.append((backend, model))
        return {0: "Orders"}, "llm"

    monkeypatch.setattr("graphify.llm.generate_community_labels", fake_generate)
    monkeypatch.setattr("graphify.export.to_html", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "label", str(tmp_path), "--no-viz", *extra])
    cli.main()
    return calls


def test_community_naming_skips_llm_when_none_selected(no_backend, tmp_path, monkeypatch, capsys):
    calls = _run_label(_graph_dir(tmp_path), monkeypatch)
    assert calls == []
    err = capsys.readouterr().err
    assert "community naming (1 communities) can use an LLM, but none is selected" in err
    labels = json.loads((tmp_path / "graphify-out" / ".graphify_labels.json").read_text(encoding="utf-8"))
    assert labels["0"] != "Community 0"  # hub-based name, not a placeholder


def test_community_naming_uses_selected_claude_cli_and_announces(no_backend, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    calls = _run_label(_graph_dir(tmp_path), monkeypatch)
    assert calls == [("claude-cli", "sonnet")]
    assert "calling LLM via claude-cli" in capsys.readouterr().err


def test_extract_with_docs_and_no_llm_explains_options(no_backend, tmp_path, monkeypatch, capsys):
    import graphify.__main__ as cli
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (corpus / "notes.md").write_text("# Notes\n\nSome design notes.\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # present, but not selected
    monkeypatch.setattr(cli, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "extract", str(corpus), "--out", str(tmp_path / "o")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "none is selected" in err and "--code-only" in err and "GRAPHIFY_BACKEND" in err


def test_call_llm_claude_cli_uses_the_model_variable(no_backend, monkeypatch):
    from graphify import llm
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setenv("GRAPHIFY_CLAUDE_CLI_MODEL", "sonnet")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", fake_run)
    llm._call_llm("hello", backend="claude-cli")
    args = captured["args"]
    assert args[args.index("--model") + 1] == "sonnet"


def test_pr_triage_is_skipped_without_a_selected_backend(no_backend, monkeypatch):
    from graphify import prs
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert prs._resolve_triage_backend() is None
    monkeypatch.setenv("GRAPHIFY_BACKEND", "claude-cli")
    assert prs._resolve_triage_backend() == ("claude-cli", "sonnet", "GRAPHIFY_BACKEND")
