"""Fork additions to `graphify affected` shared by the CLI and the MCP tool.

Upstream's ``DEFAULT_AFFECTED_RELATIONS`` covers code relations only. On a graph
that carries this fork's layers, the default blast radius also follows the IaC
dependency relations and git co-change coupling; on any other graph the
relation set, and so the output, is exactly upstream's.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from graphify import cochange, iac_link
from graphify.affected import DEFAULT_AFFECTED_RELATIONS

__all__ = ["prepare", "default_relations"]


def default_relations(G: nx.Graph) -> tuple[str, ...]:
    rels = iac_link.affected_relations(G, tuple(DEFAULT_AFFECTED_RELATIONS))
    return cochange.affected_relations(G, rels)


def prepare(G: nx.Graph, graph_path: "str | Path") -> nx.Graph:
    """*G* with the sibling co-change layer overlaid, when one exists."""
    return cochange.with_cochange_overlay(G, graph_path)
