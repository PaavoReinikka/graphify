"""MCP server behaviour added by this fork: the `affected` tool (IaC-aware
blast radius), the per-tier confidence breakdown, and get_node's IaC summary.

Drives the real server through mcp's in-memory transport, so tool
registration, argument handling and the text contract are all exercised.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from graphify.serve import _build_server  # noqa: E402


def _node(nid, label, sf, loc="L1", **extra):
    return {"id": nid, "label": label, "source_file": sf, "source_location": loc,
            "file_type": "code", **extra}


def _edge(s, t, rel, conf="EXTRACTED", **extra):
    return {"source": s, "target": t, "relation": rel, "confidence": conf, **extra}


def _write_graph(out: Path, *, statistical_edge: bool = False) -> Path:
    nodes = [
        _node("a", "a.py", "src/a.py"),
        _node("b", "b.py", "src/b.py"),
        _node("c", "c.py", "src/c.py"),
        _node("sa", "resource sa [Microsoft.Storage/storageAccounts@2023-01-01]",
              "infra/main.bicep", "L3", iac_lang="bicep", iac_kind="resource",
              iac_type="Microsoft.Storage/storageAccounts", iac_env="dev"),
        _node("mod", "storage.bicep", "infra/modules/storage.bicep", "L1",
              iac_lang="bicep"),
        _node("call", "module storage", "infra/main.bicep", "L5",
              iac_lang="bicep", iac_kind="module"),
    ]
    links = [_edge("b", "a", "imports"), _edge("call", "mod", "deploys")]
    if statistical_edge:  # e.g. a graph augmented by an external tool
        links.append(_edge("a", "c", "co_changes_with", "STATISTICAL", p_raw=0.001))
    out.mkdir(parents=True, exist_ok=True)
    graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": links}
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return out / "graph.json"


def _call(graph_path: Path, calls: list[tuple[str, dict]]) -> list[str]:
    server = _build_server(str(graph_path))

    async def run():
        async with create_connected_server_and_client_session(server) as s:
            names = {t.name for t in (await s.list_tools()).tools}
            assert "affected" in names
            out = []
            for name, args in calls:
                if name.startswith("graphify://"):
                    res = await s.read_resource(name)
                    out.append(res.contents[0].text)
                else:
                    res = await s.call_tool(name, args)
                    out.append("\n".join(c.text for c in res.content))
            return out

    return asyncio.run(run())


def test_affected_structural_blast_radius(tmp_path):
    gp = _write_graph(tmp_path / "graphify-out")
    (text,) = _call(gp, [("affected", {"target": "src/a.py"})])
    assert "b.py [imports]" in text and "c.py" not in text


def test_affected_on_iac_follows_deploys(tmp_path):
    gp = _write_graph(tmp_path / "graphify-out")
    (text,) = _call(gp, [("affected", {"target": "infra/modules/storage.bicep"})])
    assert "Affected nodes for storage.bicep" in text
    assert "module storage [deploys]" in text


def test_stats_and_audit_count_every_confidence_tier(tmp_path):
    gp = _write_graph(tmp_path / "graphify-out", statistical_edge=True)
    stats, audit = _call(gp, [("graph_stats", {}), ("graphify://audit", {})])
    assert "STATISTICAL: 33%" in stats and "EXTRACTED: 67%" in stats
    assert "STATISTICAL: 1 (33%)" in audit and "Total edges: 3" in audit


def test_stats_format_unchanged_for_core_tiers_only(tmp_path):
    gp = _write_graph(tmp_path / "graphify-out")
    (stats,) = _call(gp, [("graph_stats", {})])
    assert stats.splitlines()[-3:] == ["EXTRACTED: 100%", "INFERRED: 0%", "AMBIGUOUS: 0%"]


def test_get_node_shows_iac_summary(tmp_path):
    gp = _write_graph(tmp_path / "graphify-out")
    iac, plain = _call(gp, [("get_node", {"label": "sa"}), ("get_node", {"label": "a.py"})])
    assert "IaC: lang=bicep kind=resource type=Microsoft.Storage/storageAccounts env=dev" in iac
    assert "IaC:" not in plain
