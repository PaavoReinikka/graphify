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


def _disabled() -> bool:
    return os.environ.get("GRAPHIFY_NO_IAC_LINK", "").strip().lower() in ("1", "true", "yes")


def link_iac(G: nx.Graph) -> nx.Graph:
    """Add resource-type hub nodes + ``instance_of`` edges. Returns the same graph.

    Mutates ``G`` in place (and returns it for convenience). A no-op when disabled
    or when the graph holds no IaC nodes.
    """
    if _disabled():
        return G

    # Instances = resource/data nodes carrying a concrete iac_type, excluding the
    # hub nodes a prior run may have added (idempotency).
    by_type: dict[str, list[str]] = defaultdict(list)
    for nid, data in G.nodes(data=True):
        itype = data.get("iac_type")
        if not itype or data.get("iac_kind") == _HUB_KIND:
            continue
        by_type[itype].append(nid)

    if not by_type:
        return G  # not an IaC graph — leave it byte-identical

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
