"""PROTOTYPE bicep extractor for graphify.

Self-contained proof-of-concept that mirrors `graphify.extract.extract_terraform`
exactly, so it can be dropped into `graphify/extract.py` with only the
`_make_id` import swapped for the module-level helper. Run directly to see it
extract the investigation/ sample files.

Tree-sitter node types were discovered empirically (see investigation/findings.md):
  infrastructure                              -- root
  parameter_declaration  -> param  <id> <type> [= <expr>]
  variable_declaration   -> var    <id>       = <expr>
  resource_declaration   -> resource <id> '<Type@api>' [existing] = <object|for_statement>
  module_declaration     -> module   <id> '<./path.bicep>'        = <object>
  output_declaration     -> output   <id> <type>                  = <expr>
  references appear as bare `identifier` nodes and `member_expression` heads;
  `dependsOn: [ <id> ... ]` is an object_property whose value is an `array`;
  `parent: <id>` nests a resource; `for_statement.initializer` is the loop var.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path


# ---- mirror of graphify.ids.make_id (so this runs standalone) ---------------
def _normalize_id(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").casefold()


def _make_id(*parts: str) -> str:
    return _normalize_id("_".join(p.strip("_.") for p in parts if p))
# -----------------------------------------------------------------------------


# Decorator/function heads and loop-builtins that are never references to a
# declared symbol in the file. Name-based resolution against the declared-symbol
# set already excludes most builtins; this is a belt-and-braces guard.
_BICEP_BUILTINS = frozenset({
    "resourceGroup", "subscription", "tenant", "deployment", "managementGroup",
    "range", "concat", "format", "union", "if", "json", "loadTextContent",
    "loadFileAsBase64", "reference", "resourceId", "guid", "uniqueString",
})


def extract_bicep(path: Path) -> dict:
    """Extract bicep declarations and the references between them via tree-sitter.

    Nodes: parameters, variables, resources, modules, outputs.
    Edges: `contains` (file -> declaration), `references` (declaration -> the
    symbols it interpolates), `depends_on` (explicit dependsOn arrays), `parent`
    (resource nesting via the `parent:` property), and `deploys` (module -> the
    referenced .bicep file, cross-file like Terraform's module source).

    Node IDs are scoped by the *file* path, because bicep is file-scoped: a
    single .bicep file is one deployment template and symbol names are unique
    within it. Cross-file links happen only through `module` path references.
    """
    try:
        import tree_sitter_bicep as tsb
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [],
                "error": "tree_sitter_bicep not installed. Run: pip install tree-sitter-bicep"}

    try:
        language = Language(tsb.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:  # pragma: no cover - defensive
        return {"nodes": [], "edges": [], "error": str(e)}

    str_path = str(path)
    file_nid = _make_id(str_path)
    scope = str_path  # file-scoped IDs

    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                          "source_file": str_path, "source_location": None}]
    edges: list[dict] = []
    seen_ids: set[str] = {file_nid}
    seen_edges: set[tuple[str, str, str]] = set()
    # name -> node id, for name-based reference resolution (declared symbols only)
    symbols: dict[str, str] = {}

    def _read(n) -> str:
        return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    def _add_node(name: str, label: str, line: int) -> str:
        nid = _make_id(scope, name)
        symbols[name] = nid
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})
            edges.append({"source": file_nid, "target": nid, "relation": "contains",
                          "confidence": "EXTRACTED", "source_file": str_path,
                          "source_location": f"L{line}", "weight": 1.0})
        return nid

    def _add_edge(src: str, tgt: str, relation: str, line: int) -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, relation)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": "EXTRACTED", "source_file": str_path,
                      "source_location": f"L{line}", "weight": 1.0})

    def _decl_name(decl):
        """First `identifier` child is the declared symbol name."""
        for c in decl.children:
            if c.type == "identifier":
                return _read(c), c.start_point[0] + 1
        return None, None

    def _type_label(decl) -> str:
        """The single-quoted type string of a resource/module, if present."""
        for c in decl.children:
            if c.type == "string":
                for gc in c.children:
                    if gc.type == "string_content":
                        return _read(gc)
        return ""

    def _body(decl):
        for c in decl.children:
            if c.type in ("object", "for_statement", "if_statement"):
                return c
        return None

    # ---- pass 1: declare every top-level symbol so references can resolve ----
    decls = [c for c in root.children if c.type.endswith("_declaration")]
    meta: list = []
    for decl in decls:
        name, line = _decl_name(decl)
        if not name:
            continue
        if decl.type == "parameter_declaration":
            owner = _add_node(name, f"param {name}", line)
        elif decl.type == "variable_declaration":
            owner = _add_node(name, f"var {name}", line)
        elif decl.type == "resource_declaration":
            tlabel = _type_label(decl)
            is_existing = any(c.type == "existing" for c in decl.children)
            suffix = " (existing)" if is_existing else ""
            owner = _add_node(name, f"resource {name} [{tlabel}]{suffix}", line)
        elif decl.type == "module_declaration":
            owner = _add_node(name, f"module {name}", line)
            # cross-file deploy edge to the referenced .bicep file
            modpath = _type_label(decl)
            if modpath:
                target_file = (path.parent / modpath).resolve()
                _add_edge(owner, _make_id(str(target_file)), "deploys", line)
        elif decl.type == "output_declaration":
            owner = _add_node(name, f"output {name}", line)
        else:
            continue
        meta.append((decl, owner))

    # ---- pass 2: walk each body, emit references / depends_on / parent -------
    def _walk(node, owner_nid: str, relation: str, loop_vars: frozenset):
        ntype = node.type

        # `for i in ...:` introduces a loop variable that is not a reference.
        if ntype == "for_statement":
            init = node.child_by_field_name("initializer")
            lv = loop_vars
            if init is not None and init.type == "identifier":
                lv = loop_vars | {_read(init)}
            for c in node.children:
                if c.is_named:
                    _walk(c, owner_nid, relation, lv)
            return

        if ntype == "object_property":
            key = node.children[0] if node.children else None
            keyname = _read(key) if key is not None else ""
            if keyname == "dependsOn":
                for c in node.children:
                    if c.type == "array":
                        for el in c.children:
                            if el.type == "identifier":
                                nm = _read(el)
                                if nm in symbols:
                                    _add_edge(owner_nid, symbols[nm], "depends_on",
                                              el.start_point[0] + 1)
                return
            if keyname == "parent":
                # value is the parent resource identifier
                for c in node.children[1:]:
                    if c.type == "identifier":
                        nm = _read(c)
                        if nm in symbols:
                            _add_edge(owner_nid, symbols[nm], "parent",
                                      c.start_point[0] + 1)
                return

        # a bare identifier (or the head of a member_expression like `storage.id`)
        if ntype == "identifier":
            nm = _read(node)
            if nm in symbols and nm not in loop_vars:
                _add_edge(owner_nid, symbols[nm], relation, node.start_point[0] + 1)
            return

        if ntype == "member_expression":
            obj = node.child_by_field_name("object")
            if obj is not None:
                _walk(obj, owner_nid, relation, loop_vars)
            return  # don't descend into the .property chain

        for c in node.children:
            if c.is_named:
                _walk(c, owner_nid, relation, loop_vars)

    for decl, owner in meta:
        body = _body(decl)
        if body is not None:
            _walk(body, owner, "references", frozenset())
        # default-value expressions of params/outputs live after `=` too
        if decl.type in ("parameter_declaration", "output_declaration"):
            for c in decl.children:
                if c.type not in ("identifier", "type", "param", "output", "=",
                                  "object", "for_statement"):
                    if c.is_named:
                        _walk(c, owner, "references", frozenset())

    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    import json
    for fn in ("sample.bicep", "sample2.bicep"):
        p = Path(__file__).parent / fn
        if not p.exists():
            continue
        r = extract_bicep(p)
        print(f"\n===== {fn} =====")
        if r.get("error"):
            print("ERROR:", r["error"]); continue
        print(f"{len(r['nodes'])} nodes, {len(r['edges'])} edges")
        lab = {n["id"]: n["label"] for n in r["nodes"]}
        for n in r["nodes"]:
            print(f"  NODE {n['label']!r}  ({n['source_location']})")
        for e in r["edges"]:
            print(f"  EDGE {lab.get(e['source'], e['source'])!r} "
                  f"--{e['relation']}--> {lab.get(e['target'], e['target'])!r}")
