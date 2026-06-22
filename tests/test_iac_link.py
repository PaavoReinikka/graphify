"""Tests for the IaC linking pass (graphify/iac_link.py) — Stage 2: type hubs."""
from __future__ import annotations

import networkx as nx
import pytest

from graphify.build import build_from_json
from graphify.iac_link import link_iac
from graphify.ids import make_id


def _resource(nid, itype, *, lang="bicep"):
    return {"id": nid, "label": nid, "file_type": "code", "source_file": f"{nid}.bicep",
            "source_location": "L1", "iac_lang": lang, "iac_kind": "resource",
            "iac_type": itype}


def _hub_id(itype):
    return make_id("iac_type", itype)


def test_hub_created_for_repeated_type():
    # Three storage accounts across files -> exactly one hub with three
    # instance_of edges.
    nodes = [_resource(f"sa{i}", "Microsoft.Storage/storageAccounts") for i in range(3)]
    G = build_from_json({"nodes": nodes, "edges": []})
    hub = _hub_id("Microsoft.Storage/storageAccounts")
    assert hub in G
    assert G.nodes[hub]["file_type"] == "concept"
    instance_edges = [(u, v) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "instance_of" and v == hub]
    assert len(instance_edges) == 3


def test_singleton_type_gets_no_hub():
    G = build_from_json({"nodes": [_resource("sa0", "Microsoft.Storage/storageAccounts")],
                         "edges": []})
    assert _hub_id("Microsoft.Storage/storageAccounts") not in G


def test_idempotent_on_rerun():
    nodes = [_resource(f"sa{i}", "Microsoft.Storage/storageAccounts") for i in range(3)]
    G = build_from_json({"nodes": nodes, "edges": []})
    n1, e1 = G.number_of_nodes(), G.number_of_edges()
    # second pass must not add a hub-of-a-hub or duplicate instance_of edges
    link_iac(G)
    assert (G.number_of_nodes(), G.number_of_edges()) == (n1, e1)
    # the hub is not treated as an instance of its own type
    hub = _hub_id("Microsoft.Storage/storageAccounts")
    assert not G.has_edge(hub, hub)


def test_non_iac_graph_untouched():
    nodes = [{"id": "a", "label": "a", "file_type": "code", "source_file": "a.py",
              "source_location": "L1"},
             {"id": "b", "label": "b", "file_type": "code", "source_file": "b.py",
              "source_location": "L1"}]
    edges = [{"source": "a", "target": "b", "relation": "calls",
              "confidence": "EXTRACTED", "source_file": "a.py"}]
    G = build_from_json({"nodes": nodes, "edges": edges})
    assert set(G.nodes) == {"a", "b"}
    assert not any(d.get("relation") == "instance_of" for _, _, d in G.edges(data=True))


def test_opt_out_env(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_NO_IAC_LINK", "1")
    nodes = [_resource(f"sa{i}", "Microsoft.Storage/storageAccounts") for i in range(3)]
    G = build_from_json({"nodes": nodes, "edges": []})
    assert _hub_id("Microsoft.Storage/storageAccounts") not in G


def test_mixed_types_get_separate_hubs():
    nodes = (
        [_resource(f"sa{i}", "Microsoft.Storage/storageAccounts") for i in range(2)]
        + [_resource(f"kv{i}", "Microsoft.KeyVault/vaults") for i in range(2)]
    )
    G = build_from_json({"nodes": nodes, "edges": []})
    assert _hub_id("Microsoft.Storage/storageAccounts") in G
    assert _hub_id("Microsoft.KeyVault/vaults") in G


def test_terraform_and_bicep_share_type_hub():
    # Same provider type from both languages should land under one hub.
    nodes = [
        _resource("tf_a", "aws_instance", lang="terraform"),
        _resource("tf_b", "aws_instance", lang="terraform"),
    ]
    G = build_from_json({"nodes": nodes, "edges": []})
    hub = _hub_id("aws_instance")
    assert hub in G
    assert G.degree(hub) == 2


def _resource_at(nid, itype, source_file):
    return {"id": nid, "label": nid, "file_type": "code", "source_file": source_file,
            "source_location": "L1", "iac_lang": "bicep", "iac_kind": "resource",
            "iac_type": itype}


@pytest.mark.parametrize("path,env", [
    ("environments/prod/main.bicep", "prod"),
    ("environments/dev/main.bicep", "dev"),
    ("infra/staging/storage.bicep", "staging"),
    ("envs/production/x.bicep", "prod"),       # token normalization
    ("environments/edge/x.bicep", "edge"),     # custom env after anchor
    ("modules/network/main.bicep", None),      # no env segment
    ("prd/main.bicep", "prod"),
])
def test_env_derivation(path, env):
    from graphify.iac_link import _derive_env, _path_parts
    assert _derive_env(_path_parts(path)) == env


@pytest.mark.parametrize("path,layer", [
    ("modules/network/main.bicep", "module"),
    ("core/identity/main.bicep", "core"),
    ("common/naming.bicep", "core"),
    ("apps/api/main.bicep", "app"),
    ("services/worker/main.bicep", "app"),
    ("environments/prod/main.bicep", None),
    ("modules/core/main.bicep", "module"),     # module dir wins over core label
])
def test_layer_derivation(path, layer):
    from graphify.iac_link import _derive_layer, _path_parts
    assert _derive_layer(_path_parts(path)) == layer


def test_scoping_attributes_set_on_build():
    nodes = [
        _resource_at("prodsa", "Microsoft.Storage/storageAccounts",
                     "environments/prod/storage.bicep"),
        _resource_at("devsa", "Microsoft.Storage/storageAccounts",
                     "environments/dev/storage.bicep"),
        _resource_at("modnsg", "Microsoft.Network/networkSecurityGroups",
                     "modules/network/nsg.bicep"),
    ]
    G = build_from_json({"nodes": nodes, "edges": []})
    assert G.nodes["prodsa"]["iac_env"] == "prod"
    assert G.nodes["devsa"]["iac_env"] == "dev"
    assert G.nodes["modnsg"]["iac_layer"] == "module"
    # prod and dev storage stay distinguishable but still share the type hub
    hub = _hub_id("Microsoft.Storage/storageAccounts")
    assert hub in G and G.degree(hub) == 2


def test_scoping_does_not_break_idempotency():
    nodes = [_resource_at(f"sa{i}", "Microsoft.Storage/storageAccounts",
                          f"environments/prod/sa{i}.bicep") for i in range(2)]
    G = build_from_json({"nodes": nodes, "edges": []})
    counts = (G.number_of_nodes(), G.number_of_edges())
    link_iac(G)
    assert (G.number_of_nodes(), G.number_of_edges()) == counts


def test_multidir_module_deploys_resolves():
    # A prod stack deploying a reusable module in another directory: the bicep
    # deploys edge must resolve to the real module file node after the full
    # extract() pipeline (directory-spanning monorepo layout).
    import tempfile
    from pathlib import Path
    from graphify.extract import extract

    d = Path(tempfile.mkdtemp())
    (d / "environments" / "prod").mkdir(parents=True)
    (d / "modules" / "network").mkdir(parents=True)
    (d / "environments" / "prod" / "main.bicep").write_text(
        "module net '../../modules/network/main.bicep' = {\n  name: 'n'\n}\n",
        encoding="utf-8")
    (d / "modules" / "network" / "main.bicep").write_text(
        "param location string\noutput vnetId string = location\n", encoding="utf-8")

    paths = [d / "environments" / "prod" / "main.bicep",
             d / "modules" / "network" / "main.bicep"]
    res = extract(paths, cache_root=d)
    G = build_from_json({"nodes": res["nodes"], "edges": res["edges"]}, root=d)
    deploys = [(u, v) for u, v, dd in G.edges(data=True) if dd.get("relation") == "deploys"]
    assert deploys, "no deploys edge emitted"
    for _, v in deploys:
        assert v in G.nodes, f"deploys target {v} is dangling"


def test_hub_groups_instances_under_clustering():
    # The instance_of edges to a type hub should pull all instances into one
    # community (that's the point of the hub: a god-node for the type).
    from graphify.cluster import cluster
    nodes = [_resource(f"sa{i}", "Microsoft.Storage/storageAccounts") for i in range(4)]
    G = build_from_json({"nodes": nodes, "edges": []})
    hub = _hub_id("Microsoft.Storage/storageAccounts")
    communities = cluster(G)  # {community_id: [node_ids]}
    node_to_comm = {n: cid for cid, members in communities.items() for n in members}
    hub_comm = node_to_comm[hub]
    for i in range(4):
        assert node_to_comm[f"sa{i}"] == hub_comm
