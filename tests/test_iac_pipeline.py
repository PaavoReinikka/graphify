"""End-to-end checks for the fork's IaC layer on top of upstream's pipeline.

Unit tests (test_bicep / test_iac_link) exercise the extractor and the link pass
in isolation; these run the real ``extract()`` -> ``build_from_json`` path so the
integration points with upstream stay covered: dispatch registration, the
portable-id remap of Bicep ids, and link_iac annotating upstream's (unmodified)
Terraform extractor output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_bicep")
pytest.importorskip("tree_sitter_hcl")

from graphify.build import build_from_json  # noqa: E402
from graphify.extract import extract  # noqa: E402

MAIN_BICEP = """\
param location string = resourceGroup().location

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: { location: location }
}

output storageEndpoint string = storage.outputs.endpoint
"""

STORAGE_BICEP = """\
param location string

resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'st${uniqueString(resourceGroup().id)}'
  location: location
}

output endpoint string = sa.properties.primaryEndpoints.blob
"""

MAIN_TF = """\
variable "region" { default = "eu-north-1" }

locals { size = "t3.micro" }

data "aws_ami" "ubuntu" { most_recent = true }

resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = local.size
}

resource "aws_instance" "worker" {
  ami = data.aws_ami.ubuntu.id
}

output "worker_address" { value = aws_instance.worker.private_ip }
"""

APP_PY = """\
def worker_address():
    return "10.0.0.1"
"""


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def graph(tmp_path):
    # app/ first, as a sorted directory walk yields it: app nodes then precede
    # the IaC nodes, which is the order that exposes lost edge direction.
    files = [
        _write(tmp_path, "app/client.py", APP_PY),
        _write(tmp_path, "infra/main.bicep", MAIN_BICEP),
        _write(tmp_path, "infra/modules/storage.bicep", STORAGE_BICEP),
        _write(tmp_path, "tf/main.tf", MAIN_TF),
    ]
    # Per-test cache (like the CLI's per-project graphify-out/): the default
    # cache_root is CWD, which would share entries across tests' temp roots.
    result = extract(files, cache_root=tmp_path, root=tmp_path, parallel=False)
    return build_from_json(result, root=tmp_path)


def _by_label(G, label: str) -> tuple[str, dict]:
    matches = [(n, d) for n, d in G.nodes(data=True) if d.get("label") == label]
    assert len(matches) == 1, f"{label!r}: {matches}"
    return matches[0]


def test_bicep_ids_are_portable(graph, tmp_path):
    # No node id may embed the absolute scan path: the id remap must cover the
    # file-scoped Bicep symbol prefix exactly as it does for other languages.
    from graphify.ids import normalize_id
    abs_slug = normalize_id(str(tmp_path.resolve()))
    bicep_ids = [n for n, d in graph.nodes(data=True) if d.get("iac_lang") == "bicep"]
    assert bicep_ids
    for nid in bicep_ids:
        assert abs_slug not in nid and nid.startswith("infra_"), nid


def test_bicep_module_deploys_resolves_to_file_node(graph):
    mod, _ = _by_label(graph, "module storage")
    targets = [v for _, v, d in graph.edges(mod, data=True) if d.get("relation") == "deploys"]
    assert len(targets) == 1
    target = graph.nodes[targets[0]]
    assert target.get("source_file", "").replace("\\", "/").endswith("modules/storage.bicep")
    assert target.get("label") == "storage.bicep"


def test_terraform_nodes_are_annotated_by_link_iac(graph):
    expected = {
        "aws_instance.web": ("resource", "aws_instance", "web"),
        "data.aws_ami.ubuntu": ("data", "aws_ami", "ubuntu"),
        "var.region": ("param", None, "region"),
        "local.size": ("var", None, "size"),
        "output.worker_address": ("output", None, "worker_address"),
    }
    for label, (kind, itype, name) in expected.items():
        _, d = _by_label(graph, label)
        assert d["iac_lang"] == "terraform", label
        assert d["iac_kind"] == kind, label
        assert d.get("iac_type") == itype, label
        assert d["iac_name"] == name, label
    # the file node and upstream's directory anchor stay unannotated
    for _, d in graph.nodes(data=True):
        if d.get("type") in ("terraform_file", "module"):
            assert "iac_lang" not in d


def test_terraform_resource_type_hub(graph):
    hub, d = _by_label(graph, "aws_instance")
    assert d["iac_kind"] == "resource_type"
    members = {graph.nodes[u if v == hub else v]["label"]
               for u, v, e in graph.edges(data=True)
               if e.get("relation") == "instance_of" and hub in (u, v)}
    assert members == {"aws_instance.web", "aws_instance.worker"}


def test_terraform_output_linked_to_app_consumer(graph):
    out, _ = _by_label(graph, "output.worker_address")
    linked = [
        graph.nodes[v if u == out else u]
        for u, v, e in graph.edges(data=True)
        if e.get("relation") == "consumed_by" and out in (u, v)
    ]
    assert any(n.get("source_file", "").endswith("client.py") for n in linked)


def test_link_iac_edges_keep_direction_in_graph_json(graph, tmp_path):
    # build_from_json graphs are undirected; link_iac edges must carry
    # _src/_tgt so to_json writes output -> app and instance -> hub.
    import json
    from graphify.export import to_json
    out = tmp_path / "graph.json"
    to_json(graph, {}, str(out), force=True)
    data = json.loads(out.read_text(encoding="utf-8"))
    labels = {n["id"]: n.get("label") for n in data["nodes"]}
    by_rel = {}
    for e in data["links"]:
        by_rel.setdefault(e["relation"], []).append((labels[e["source"]], labels[e["target"]]))
    assert ("output.worker_address", "worker_address()") in by_rel["consumed_by"]
    assert all(t == "aws_instance" for s, t in by_rel["instance_of"] if s.startswith("aws_instance."))


def test_affected_file_seed_resolves_to_the_iac_file_node(graph):
    # A block declared on line 1 must not win over the file node itself when
    # `affected` is given a Bicep / Terraform file path.
    from graphify.affected import resolve_seed
    for path, label in (("infra/modules/storage.bicep", "storage.bicep"),
                        ("tf/main.tf", "main.tf")):
        seed = resolve_seed(graph, path)
        assert graph.nodes[seed]["label"] == label, (path, graph.nodes[seed])


def test_affected_on_a_bicep_module_reaches_the_modules_deploying_it(graph, tmp_path):
    # Through the real path: graph.json on disk, loaded directed as the CLI and
    # the MCP server do, so stored edge direction is what gets traversed.
    from graphify import fork_affected
    from graphify.affected import affected_nodes, load_graph, resolve_seed
    from graphify.export import to_json
    out = tmp_path / "graph.json"
    to_json(graph, {}, str(out), force=True)
    G = load_graph(out)
    seed = resolve_seed(G, "infra/modules/storage.bicep")
    hits = {G.nodes[h.node_id]["label"]: h.via_relation
            for h in affected_nodes(G, seed, relations=fork_affected.default_relations(G))}
    assert hits.get("module storage") == "deploys"
    assert "output storageEndpoint" in hits  # references the deploying module


def test_default_relations_unchanged_for_non_iac_graphs():
    import networkx as nx
    from graphify import fork_affected
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS
    G = nx.DiGraph()
    G.add_node("a", label="a.py", source_file="a.py", source_location="L1")
    assert fork_affected.default_relations(G) == tuple(DEFAULT_AFFECTED_RELATIONS)
