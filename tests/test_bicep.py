"""Tests for the Bicep extractor (graphify/extract.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.build import build_from_json
from graphify.extract import extract_bicep

FIXTURE = Path(__file__).parent / "fixtures" / "sample.bicep"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _labels(r) -> set[str]:
    return {n["label"] for n in r["nodes"]}


def _rel_pairs(r, relation: str) -> set[tuple[str, str]]:
    lab = {n["id"]: n["label"] for n in r["nodes"]}
    return {
        (lab.get(e["source"], e["source"]), lab.get(e["target"], e["target"]))
        for e in r["edges"]
        if e["relation"] == relation
    }


def _node_by_label(r, label: str) -> dict:
    return next(n for n in r["nodes"] if n["label"] == label)


def test_fixture_extracts_without_error():
    r = extract_bicep(FIXTURE)
    assert r.get("error") is None
    assert len(r["nodes"]) > 1


def test_all_declaration_kinds_become_nodes(tmp_path):
    r = extract_bicep(FIXTURE)
    labels = _labels(r)
    for expected in (
        "param location",
        "param storageName",
        "var storageSku",
        "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]",
        "module networking",
        "output storageId",
    ):
        assert expected in labels, f"missing node {expected!r}"


def test_reference_edges_including_nested_and_member_heads():
    r = extract_bicep(FIXTURE)
    refs = _rel_pairs(r, "references")
    storage = "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]"
    # direct property reference
    assert (storage, "param storageName") in refs
    assert (storage, "param location") in refs
    # reference nested inside the sku{} object resolves
    assert (storage, "var storageSku") in refs
    # module params reference a param
    assert ("module networking", "param location") in refs
    # output reads `storage.id` — the member_expression head resolves, `.id` ignored
    assert ("output storageId", storage) in refs


def test_depends_on_edges():
    r = extract_bicep(FIXTURE)
    deps = _rel_pairs(r, "depends_on")
    nic = "resource nic [Microsoft.Network/networkInterfaces@2023-01-01]"
    storage = "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]"
    assert (nic, storage) in deps
    # dependsOn inside a for-loop body still resolves
    vms = "resource vms [Microsoft.Compute/virtualMachines@2023-01-01]"
    assert (vms, nic) in deps


def test_parent_edge():
    r = extract_bicep(FIXTURE)
    parents = _rel_pairs(r, "parent")
    blob = "resource blob [Microsoft.Storage/storageAccounts/blobServices@2023-01-01]"
    storage = "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]"
    assert (blob, storage) in parents


def test_string_interpolation_reference_resolves():
    # `name: 'nic-${location}'` references the location param via interpolation.
    r = extract_bicep(FIXTURE)
    refs = _rel_pairs(r, "references")
    nic = "resource nic [Microsoft.Network/networkInterfaces@2023-01-01]"
    assert (nic, "param location") in refs


def test_loop_variable_and_builtins_not_emitted():
    r = extract_bicep(FIXTURE)
    ref_targets = {t for _, t in _rel_pairs(r, "references")}
    # the loop var `i` and the builtins range()/resourceGroup() are not symbols
    assert not any(t in ("i", "range", "resourceGroup") for t in ref_targets)


def test_existing_resource_flagged():
    r = extract_bicep(FIXTURE)
    labels = _labels(r)
    assert any("existing" in lab for lab in labels)


def test_module_deploys_edge_present():
    r = extract_bicep(FIXTURE)
    assert any(e["relation"] == "deploys" for e in r["edges"])


def test_iac_annotations_present():
    r = extract_bicep(FIXTURE)
    storage = _node_by_label(
        r, "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]")
    assert storage["iac_lang"] == "bicep"
    assert storage["iac_kind"] == "resource"
    assert storage["iac_type"] == "Microsoft.Storage/storageAccounts"  # version stripped
    param = _node_by_label(r, "param location")
    assert param["iac_kind"] == "param"
    module = _node_by_label(r, "module networking")
    assert module["iac_kind"] == "module"
    assert module["iac_path"] == "./modules/network.bicep"


def test_file_contains_declarations():
    r = extract_bicep(FIXTURE)
    contains = _rel_pairs(r, "contains")
    assert ("sample.bicep", "param location") in contains
    assert ("sample.bicep",
            "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]") in contains


def test_empty_and_comment_only_files_are_safe(tmp_path):
    assert extract_bicep(_write(tmp_path, "a.bicep", "")).get("error") is None
    r = extract_bicep(_write(tmp_path, "b.bicep", "// just a comment\n"))
    assert len(r["nodes"]) == 1  # only the file node, no crash


def test_edges_resolve_in_built_graph():
    # Every reference/depends_on/parent target must be a real node in the graph
    # (no dangling intra-file edges) once built.
    r = extract_bicep(FIXTURE)
    G = build_from_json({"nodes": r["nodes"], "edges": r["edges"]})
    intra = [e for e in r["edges"]
             if e["relation"] in ("references", "depends_on", "parent")]
    node_ids = set(G.nodes)
    for e in intra:
        assert e["source"] in node_ids and e["target"] in node_ids


def test_missing_grammar_returns_friendly_error(tmp_path, monkeypatch):
    # When tree-sitter-bicep is not installed, the extractor must degrade
    # gracefully (no crash) — base `pip install graphifyy` ships no bicep grammar.
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter_bicep", None)
    r = extract_bicep(_write(tmp_path, "x.bicep", "param a string\n"))
    assert r["nodes"] == [] and r["edges"] == []
    assert "tree_sitter_bicep" in r.get("error", "")


def test_build_preserves_iac_annotations():
    r = extract_bicep(FIXTURE)
    G = build_from_json({"nodes": r["nodes"], "edges": r["edges"]})
    storage_id = _node_by_label(
        r, "resource storage [Microsoft.Storage/storageAccounts@2023-01-01]")["id"]
    assert G.nodes[storage_id].get("iac_type") == "Microsoft.Storage/storageAccounts"
    assert G.nodes[storage_id].get("iac_lang") == "bicep"
