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
            extra_args=["--alpha", "0.01", "--include-deleted"],
        )
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:3] == ["/usr/bin/graphmine", "cochange", "myrepo"]
    assert cmd[cmd.index("--graphify-graph") + 1] == str(Path("graphify-out/graph.json"))
    assert cmd[cmd.index("--out") + 1] == str(Path("graphify-out"))
    # mining options are forwarded verbatim, never re-spelled by graphify
    assert cmd[-3:] == ["--alpha", "0.01", "--include-deleted"]
    # UTF-8 pipe so graphmine's non-ASCII digest never crashes on Windows cp1252
    assert captured["kwargs"].get("encoding") == "utf-8"


def test_cli_forwards_unknown_flags_and_keeps_own(tmp_path, monkeypatch):
    graph = tmp_path / "out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text("{}", encoding="utf-8")
    calls = {}

    def fake_enrich(repo, graph_path, out_dir, *, extra_args=None):
        calls.update(repo=repo, graph_path=graph_path, out_dir=out_dir, extra=extra_args)
        return 0

    monkeypatch.setattr(cochange, "enrich_with_cochange", fake_enrich)
    monkeypatch.setattr(cochange, "update_instructions", lambda d: calls.setdefault("instr", d))
    cochange.run_cli(["myrepo", "--graph", str(graph), "--subsystem-depth", "2",
                      "--update-instructions", "--significance", "tarone"])
    assert calls["repo"] == Path("myrepo")
    assert calls["graph_path"] == graph and calls["out_dir"] == graph.parent
    assert calls["extra"] == ["--subsystem-depth", "2", "--significance", "tarone"]
    assert calls["instr"] == Path("myrepo")


def test_cli_does_not_update_instructions_when_graphmine_fails(tmp_path, monkeypatch):
    graph = tmp_path / "graph.json"
    graph.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cochange, "enrich_with_cochange", lambda *a, **k: 2)
    touched = []
    monkeypatch.setattr(cochange, "update_instructions", touched.append)
    try:
        cochange.run_cli([".", "--graph", str(graph), "--update-instructions"])
    except SystemExit as e:
        assert e.code == 2
    assert touched == []


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


def _cochange_graph():
    import networkx as nx
    G = nx.DiGraph()
    for nid, sf in (("a", "src/a.py"), ("b", "src/b.py"), ("c", "src/c.py")):
        G.add_node(nid, label=sf.rsplit("/", 1)[-1], source_file=sf, source_location="L1")
    G.add_edge("b", "a", relation="imports", source_file="src/b.py", source_location="L1")
    # stored once, pointing AWAY from the seed: must still be followed from "a"
    G.add_edge("a", "c", relation="co_changes_with", confidence="STATISTICAL")
    return G


def test_affected_relations_adds_cochange_only_when_present():
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS
    import networkx as nx
    assert cochange.affected_relations(nx.DiGraph(), DEFAULT_AFFECTED_RELATIONS) == DEFAULT_AFFECTED_RELATIONS
    rels = cochange.affected_relations(_cochange_graph(), DEFAULT_AFFECTED_RELATIONS)
    assert rels == (*DEFAULT_AFFECTED_RELATIONS, "co_changes_with")


def test_affected_follows_cochange_in_both_directions():
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS, affected_nodes
    G = _cochange_graph()
    rels = cochange.affected_relations(G, DEFAULT_AFFECTED_RELATIONS)
    hits = {h.node_id: h.via_relation for h in affected_nodes(G, "a", relations=rels)}
    assert hits == {"b": "imports", "c": "co_changes_with"}
    back = {h.node_id: h.via_relation for h in affected_nodes(G, "c", relations=rels)}
    assert back["a"] == "co_changes_with" and back["b"] == "imports"
    # structural-only walk is unchanged
    assert {h.node_id for h in affected_nodes(G, "a")} == {"b"}
