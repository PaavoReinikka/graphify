"""Shared scaffolding for declarative infrastructure-as-code (IaC) extractors.

Terraform/HCL and Bicep describe the same shape — a flat set of *declarations*
(resources, modules, parameters/variables, outputs) plus *references* between
them — so they share one node/edge vocabulary and one piece of bookkeeping. This
module owns that bookkeeping; the per-language extractors in ``extract.py`` only
walk their tree-sitter grammar and call :meth:`IaCGraphBuilder.add_node` /
:meth:`IaCGraphBuilder.add_edge`.

Two scoping strategies are supported because the two languages resolve
references differently:

* **Bicep is file-scoped.** One ``.bicep`` file is one deployment template and
  symbol names are unique within it, so references are resolved by *name* against
  the declarations collected in the same file (``require_known=True``).
* **Terraform is directory-scoped.** A resource in ``main.tf`` is referenced from
  sibling ``.tf`` files, so references are resolved by *address*
  (``aws_instance.web``) and the edge is emitted unconditionally — it resolves at
  merge time when the sibling file's definition produces the same directory-scoped
  id (``require_known=False``).

The caller supplies the ``scope`` string used to namespace symbol ids. For Bicep
pass the file-node id (so symbols ride ``extract()``'s portable-id remap); for
Terraform pass the parent directory name.

Standard relation vocabulary:
    contains    file        -> declaration
    references  declaration -> a symbol it interpolates / uses
    depends_on  declaration -> an explicit dependency
    parent      resource    -> the resource it is nested under
    deploys     module      -> the file / module source it deploys
"""
from __future__ import annotations

from pathlib import Path

from .ids import make_id

__all__ = ["IaCGraphBuilder"]


class IaCGraphBuilder:
    """Accumulates IaC nodes/edges with dedup, scope-aware ids, and annotations."""

    def __init__(self, path: Path, *, lang: str, scope: str) -> None:
        self.path = path
        self.str_path = str(path)
        self.lang = lang
        self.scope = scope
        self.file_nid = make_id(self.str_path)
        self.nodes: list[dict] = [{
            "id": self.file_nid, "label": path.name, "file_type": "code",
            "source_file": self.str_path, "source_location": None,
        }]
        self.edges: list[dict] = []
        self._seen_ids: set[str] = {self.file_nid}
        self._seen_edges: set[tuple[str, str, str]] = set()
        # declared symbol name/address -> node id, for reference resolution
        self._symbols: dict[str, str] = {}

    def symbol_id(self, name: str) -> str:
        """The node id a declaration named *name* gets under the current scope."""
        return make_id(self.scope, name)

    def has_symbol(self, name: str) -> bool:
        return name in self._symbols

    def add_node(self, name: str, label: str, line: int, *, kind: str,
                 iac_type: str | None = None, iac_path: str | None = None) -> str:
        """Declare a symbol. Idempotent; emits the file ``contains`` edge once.

        Returns the node id so the caller can attach reference edges to it.
        """
        nid = self.symbol_id(name)
        self._symbols[name] = nid
        if nid not in self._seen_ids:
            self._seen_ids.add(nid)
            node: dict = {
                "id": nid, "label": label, "file_type": "code",
                "source_file": self.str_path, "source_location": f"L{line}",
                "iac_lang": self.lang, "iac_kind": kind,
                # the bare declared name (last dotted component), uniform across
                # languages: Bicep "storageId" and Terraform "output.ip" -> "ip".
                # link_iac uses it to match outputs against app-code consumers.
                "iac_name": name.split(".")[-1],
            }
            if iac_type:
                node["iac_type"] = iac_type
            if iac_path:
                node["iac_path"] = iac_path
            self.nodes.append(node)
            self.edges.append({
                "source": self.file_nid, "target": nid, "relation": "contains",
                "confidence": "EXTRACTED", "source_file": self.str_path,
                "source_location": f"L{line}", "weight": 1.0,
            })
        return nid

    def _emit_edge(self, src: str, tgt: str, relation: str, line: int) -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, relation)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append({
            "source": src, "target": tgt, "relation": relation,
            "confidence": "EXTRACTED", "source_file": self.str_path,
            "source_location": f"L{line}", "weight": 1.0,
        })

    def add_ref(self, src: str, name: str, relation: str, line: int, *,
                require_known: bool) -> None:
        """Add an edge from *src* to the symbol identified by *name*.

        ``require_known=True`` (Bicep): only emit if *name* is a declared symbol
        in this file. ``require_known=False`` (Terraform): always emit to the
        scoped id for *name* so cross-file/directory references resolve at merge.
        """
        if require_known:
            tgt = self._symbols.get(name)
            if tgt is None:
                return
        else:
            tgt = self.symbol_id(name)
        self._emit_edge(src, tgt, relation, line)

    def add_edge_to_id(self, src: str, tgt: str, relation: str, line: int) -> None:
        """Add an edge to an already-computed target id (e.g. a cross-file node)."""
        self._emit_edge(src, tgt, relation, line)

    def result(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges}
