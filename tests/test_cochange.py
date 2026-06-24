"""Tests for the optional graphmine co-change integration (graphify/cochange.py)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graphify import cochange


def test_no_op_when_graphmine_absent(capsys, tmp_path):
    with patch("graphify.cochange.shutil.which", return_value=None):
        rc = cochange.enrich_with_cochange(tmp_path, tmp_path / "graph.json", tmp_path)
    assert rc is None
    err = capsys.readouterr().err
    assert "graphmine" in err and "PATH" in err


def test_builds_expected_command_when_present(tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    with patch("graphify.cochange.shutil.which", return_value="/usr/bin/graphmine"), \
         patch("graphify.cochange.subprocess.run", fake_run):
        rc = cochange.enrich_with_cochange(
            Path("myrepo"), Path("graphify-out/graph.json"), Path("graphify-out"),
            correction="by", alpha=0.01, subsystem_depth=2, include_deleted=True,
        )
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/graphmine", "cochange"]
    assert "myrepo" in cmd
    assert "--graphify-graph" in cmd
    assert cmd[cmd.index("--graphify-graph") + 1] == str(Path("graphify-out/graph.json"))
    assert cmd[cmd.index("--correction") + 1] == "by"
    assert cmd[cmd.index("--alpha") + 1] == "0.01"
    assert cmd[cmd.index("--subsystem-depth") + 1] == "2"
    assert "--include-deleted" in cmd
    # UTF-8 pipe so graphmine's non-ASCII digest never crashes on Windows cp1252
    assert captured["kwargs"].get("encoding") == "utf-8"


_BASE_BLOCK = (
    "# Project notes\n\nSome text.\n\n"
    "## graphify\n\nThis project has a knowledge graph at graphify-out/.\n\n"
    "Rules:\n- run `graphify query`\n"
)


def test_update_instructions_adds_section_to_graphify_files(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(_BASE_BLOCK, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(_BASE_BLOCK, encoding="utf-8")
    (tmp_path / "README.md").write_text("# no graphify block here\n", encoding="utf-8")

    updated = cochange.update_instructions(tmp_path)

    assert set(updated) == {claude, tmp_path / "AGENTS.md"}
    text = claude.read_text(encoding="utf-8")
    assert cochange._COCHANGE_MARKER in text
    assert "co_changes_with" in text
    # base block preserved
    assert "This project has a knowledge graph" in text
    # the unrelated file is untouched
    assert cochange._COCHANGE_MARKER not in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_update_instructions_is_idempotent(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(_BASE_BLOCK, encoding="utf-8")
    cochange.update_instructions(tmp_path)
    once = claude.read_text(encoding="utf-8")
    cochange.update_instructions(tmp_path)
    twice = claude.read_text(encoding="utf-8")
    assert once == twice
    assert once.count(cochange._COCHANGE_MARKER) == 1


def test_update_instructions_skips_non_configured_and_absent(tmp_path):
    # CLAUDE.md exists but has no graphify block -> not touched; others absent.
    plain = tmp_path / "CLAUDE.md"
    plain.write_text("# just my notes\n", encoding="utf-8")
    updated = cochange.update_instructions(tmp_path)
    assert updated == []
    assert cochange._COCHANGE_MARKER not in plain.read_text(encoding="utf-8")


def test_graphmine_available_reflects_path():
    with patch("graphify.cochange.shutil.which", return_value=None):
        assert cochange.graphmine_available() is False
    with patch("graphify.cochange.shutil.which", return_value="/x/graphmine"):
        assert cochange.graphmine_available() is True
