"""Fork additions to `graphify affected`, shared by the CLI and the MCP tool.

Upstream's ``DEFAULT_AFFECTED_RELATIONS`` covers code relations only. On a graph
with infrastructure-as-code nodes the default blast radius also follows the IaC
dependency relations (see ``iac_link.IAC_AFFECTED_RELATIONS``); on any other
graph the relation set, and so the output, is exactly upstream's.
"""
from __future__ import annotations

import networkx as nx

from graphify import iac_link
from graphify.affected import DEFAULT_AFFECTED_RELATIONS

__all__ = ["default_relations"]


def default_relations(G: nx.Graph) -> tuple[str, ...]:
    return iac_link.affected_relations(G, tuple(DEFAULT_AFFECTED_RELATIONS))
