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


# --- infra <-> app linking (Stage 4) -------------------------------------------
# IaC `output` declarations are the explicit, published interface of a template,
# so they are the highest-signal anchor for "which app code consumes this infra".
# Matching is deliberately conservative (exact normalized name, length floor,
# stoplist, uniqueness) and every edge is INFERRED — never EXTRACTED.
_APP_LINK_MIN_LEN = 6          # skip short/generic output names
_APP_LINK_MAX_MATCHES = 5      # a name matching many app nodes is too generic
_APP_LINK_CONFIDENCE = 0.75    # contextual naming match (see how-it-works rubric)
_APP_LINK_STOPLIST = frozenset({
    "output", "result", "value", "location", "name", "id", "type", "key",
    "data", "count", "index", "enabled", "version", "status", "config",
    "string", "object", "array", "default", "params", "resource",
})


def _disabled() -> bool:
    return os.environ.get("GRAPHIFY_NO_IAC_LINK", "").strip().lower() in ("1", "true", "yes")


def _norm_token(s: str) -> str:
    from .ids import normalize_id
    return normalize_id(s)


def _link_app_consumers(G: nx.Graph, iac_node_ids: set[str]) -> None:
    """Link IaC `output` declarations to the application-code symbols that share
    their name. Emits INFERRED ``consumed_by`` edges (output -> app node).

    High-precision by design: exact normalized-name match, a length floor, a
    stoplist of generic names, and a requirement that the name be owned by exactly
    one output and match only a handful of app symbols.
    """
    # 1. consumable anchors: outputs with a specific, unique name.
    by_name: dict[str, list[str]] = defaultdict(list)
    for nid in iac_node_ids:
        data = G.nodes[nid]
        if data.get("iac_kind") != "output":
            continue
        name = (data.get("iac_name") or "").strip()
        tok = _norm_token(name)
        if len(tok) < _APP_LINK_MIN_LEN or tok in _APP_LINK_STOPLIST:
            continue
        by_name[tok].append(nid)
    anchors = {tok: ids[0] for tok, ids in by_name.items() if len(ids) == 1}
    if not anchors:
        return

    # 2. app-code symbols: code nodes that are NOT infrastructure. Index by
    #    normalized label so the match is a whole-symbol-name match, not substring.
    app_by_tok: dict[str, list[str]] = defaultdict(list)
    for nid, data in G.nodes(data=True):
        if data.get("iac_lang") or data.get("file_type") != "code":
            continue
        tok = _norm_token(str(data.get("label", "")))
        if tok in anchors:
            app_by_tok[tok].append(nid)

    # 3. emit INFERRED edges, skipping over-generic names that hit many symbols.
    for tok, out_nid in anchors.items():
        consumers = app_by_tok.get(tok, [])
        if not consumers or len(consumers) > _APP_LINK_MAX_MATCHES:
            continue
        for app_nid in consumers:
            if not G.has_edge(out_nid, app_nid):
                G.add_edge(
                    out_nid, app_nid, relation="consumed_by", confidence="INFERRED",
                    confidence_score=_APP_LINK_CONFIDENCE, weight=1.0,
                    source_file=G.nodes[app_nid].get("source_file"),
                )


def link_iac(G: nx.Graph) -> nx.Graph:
    """Enrich an infra-as-code graph in place (and return it).

    Two enrichments, both no-ops on non-IaC graphs and idempotent:

    * **monorepo scoping** — tag every IaC node with ``iac_env`` (dev/prod/...) and
      ``iac_layer`` (module/core/app) derived from its file path, so a dev vs prod
      stack and a reusable module vs an app stack stay distinguishable in queries
      and clustering;
    * **resource-type hubs** — one concept node per ``iac_type`` with 2+ instances;
    * **infra↔app linking** — INFERRED ``consumed_by`` edges from an ``output`` to
      the application-code symbol that shares its (specific, unique) name.

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

    # 3. infra <-> app linking: outputs consumed by same-named app symbols.
    _link_app_consumers(G, {nid for nid, _ in iac_nodes})
    return G
