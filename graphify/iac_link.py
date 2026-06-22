"""Second build step: enrich a merged graph with cross-cutting IaC structure.

The per-file extractors (``extract_bicep`` / ``extract_terraform``) annotate every
infrastructure node with ``iac_lang`` / ``iac_kind`` / ``iac_type``. Some structure,
though, can only be seen once *all* files are merged into one graph — e.g. that
twelve storage accounts scattered across the repo are all the same resource type.
``link_iac`` runs once on the built graph (the "second build step") and adds that
global structure.

Stage 2 adds **resource-type hub nodes**: one concept node per distinct
``iac_type`` that has two or more instances, with an ``instance_of`` edge from
each instance to the hub. The hub becomes a natural god-node — "everything that
touches Key Vault / Storage" — and pulls its instances together under clustering.

The pass is:

* **opt-out** via ``GRAPHIFY_NO_IAC_LINK=1``;
* **a no-op on non-IaC graphs** (returns the graph untouched when no node carries
  an ``iac_type``), so the overwhelming majority of corpora are unaffected;
* **idempotent** — re-running on an already-linked graph produces an identical
  graph (hub ids are deterministic, hub nodes are excluded from the instance
  scan, and edges are de-duplicated).
"""
from __future__ import annotations

import os
from collections import defaultdict

import networkx as nx

from .ids import make_id

__all__ = ["link_iac"]

# iac_kind marker for the synthetic type-hub nodes this pass creates, so a second
# run does not treat a hub as an instance of its own type.
_HUB_KIND = "resource_type"
_HUB_ID_PREFIX = "iac_type"
# A hub only earns its keep as a grouping when 2+ instances share the type;
# a lone resource would just gain a redundant parallel node.
_MIN_INSTANCES_FOR_HUB = 2

# --- monorepo scoping (Stage 3) ------------------------------------------------
# Path segments are normalized to forward slashes and lowercased before matching.
# Sensible defaults for common monorepo conventions (dev/prod, app vs core/common,
# reusable modules at any depth); heuristic and may be made configurable later.
_ENV_DIR_ANCHORS = ("environments", "environment", "envs", "env")
_ENV_TOKENS = {
    "dev": "dev", "develop": "dev", "development": "dev",
    "test": "test", "tst": "test", "testing": "test",
    "qa": "qa", "uat": "uat",
    "stage": "staging", "staging": "staging", "stg": "staging", "preprod": "staging",
    "prod": "prod", "production": "prod", "prd": "prod",
    "sandbox": "sandbox", "sbx": "sandbox", "demo": "demo",
}
# layer precedence: a reusable-module dir wins over a stack-layer label.
_LAYER_RULES = (
    ("module", {"modules", "module", "_modules", ".modules"}),
    ("core", {"core", "common", "shared", "platform", "foundation"}),
    ("app", {"app", "apps", "application", "applications", "service",
             "services", "workload", "workloads"}),
)


def _path_parts(source_file: str | None) -> list[str]:
    if not source_file:
        return []
    return [p for p in source_file.replace("\\", "/").lower().split("/") if p]


def _derive_env(parts: list[str]) -> str | None:
    # Prefer the segment right after an environments/ anchor (honours custom names
    # like environments/edge), then any standalone env token anywhere in the path.
    for i, p in enumerate(parts[:-1]):
        if p in _ENV_DIR_ANCHORS:
            nxt = parts[i + 1]
            return _ENV_TOKENS.get(nxt, nxt)
    for p in parts:
        if p in _ENV_TOKENS:
            return _ENV_TOKENS[p]
    return None


def _derive_layer(parts: list[str]) -> str | None:
    seen = set(parts)
    for layer, tokens in _LAYER_RULES:
        if seen & tokens:
            return layer
    return None


def _disabled() -> bool:
    return os.environ.get("GRAPHIFY_NO_IAC_LINK", "").strip().lower() in ("1", "true", "yes")


def link_iac(G: nx.Graph) -> nx.Graph:
    """Enrich an infra-as-code graph in place (and return it).

    Two enrichments, both no-ops on non-IaC graphs and idempotent:

    * **monorepo scoping** — tag every IaC node with ``iac_env`` (dev/prod/...) and
      ``iac_layer`` (module/core/app) derived from its file path, so a dev vs prod
      stack and a reusable module vs an app stack stay distinguishable in queries
      and clustering;
    * **resource-type hubs** — one concept node per ``iac_type`` with 2+ instances.

    A no-op when disabled or when the graph holds no IaC nodes at all.
    """
    if _disabled():
        return G

    # Every IaC declaration (any language), excluding hub nodes a prior run added.
    iac_nodes = [(nid, data) for nid, data in G.nodes(data=True)
                 if data.get("iac_lang") and data.get("iac_kind") != _HUB_KIND]
    if not iac_nodes:
        return G  # not an IaC graph — leave it byte-identical

    # 1. monorepo scoping: env + layer from the file path.
    for nid, data in iac_nodes:
        parts = _path_parts(data.get("source_file"))
        env = _derive_env(parts)
        layer = _derive_layer(parts)
        if env is not None:
            G.nodes[nid]["iac_env"] = env
        if layer is not None:
            G.nodes[nid]["iac_layer"] = layer

    # 2. resource-type hubs: group instances of the same iac_type.
    by_type: dict[str, list[str]] = defaultdict(list)
    for nid, data in iac_nodes:
        itype = data.get("iac_type")
        if itype:
            by_type[itype].append(nid)

    for itype, members in by_type.items():
        if len(members) < _MIN_INSTANCES_FOR_HUB:
            continue
        hub_id = make_id(_HUB_ID_PREFIX, itype)
        if hub_id not in G:
            G.add_node(
                hub_id, label=itype, file_type="concept", iac_kind=_HUB_KIND,
                iac_type=itype, source_file=None, source_location=None,
            )
        for nid in members:
            if not G.has_edge(nid, hub_id):
                G.add_edge(
                    nid, hub_id, relation="instance_of", confidence="EXTRACTED",
                    weight=1.0, source_file=G.nodes[nid].get("source_file"),
                )
    return G
