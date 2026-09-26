"""Azure Bicep declaration extraction (fork addition, not in upstream graphify)."""
from __future__ import annotations

import os
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id
from graphify.iac import IaCGraphBuilder


# Bicep call/decorator heads and loop builtins that are never references to a
# declared symbol. Name-based resolution against the declared-symbol set already
# excludes most of these (a builtin is not a declaration); this is a guard for
# the rare case where a loop variable or builtin shadows a declared name.
_BICEP_BUILTINS = frozenset({
    "resourceGroup", "subscription", "tenant", "deployment", "managementGroup",
    "range", "concat", "format", "union", "json", "reference", "resourceId",
    "guid", "uniqueString", "loadTextContent", "loadFileAsBase64", "loadJsonContent",
})


def extract_bicep(path: Path) -> dict:
    """Extract Bicep declarations and the references between them via tree-sitter.

    Nodes: parameters, variables, resources, modules, and outputs. Edges:
    `contains` (file -> declaration), `references` (declaration -> the symbols it
    interpolates, e.g. a resource -> the `param` in its `location:` property),
    `depends_on` (explicit `dependsOn` arrays), `parent` (resource nesting via
    the `parent:` property), and `deploys` (module -> the `.bicep` file it
    deploys).

    Bicep is *file-scoped*: one `.bicep` file is one deployment template and
    symbol names are unique within it, so references are resolved by name against
    the declarations in the same file. Symbol ids are namespaced by the file-node
    id so they ride `extract()`'s portable-id remap (matching every other code
    extractor) instead of embedding an absolute path.
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
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    b = IaCGraphBuilder(path, lang="bicep", scope=_make_id(_file_stem(path)))

    def _read(n) -> str:
        return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    def _decl_name(decl):
        """First `identifier` child is the declared symbol name + its line."""
        for c in decl.children:
            if c.type == "identifier":
                return _read(c), c.start_point[0] + 1
        return None, None

    def _type_label(decl) -> str:
        """The single-quoted type/source string of a resource/module, if any."""
        for c in decl.children:
            if c.type == "string":
                for gc in c.children:
                    if gc.type == "string_content":
                        return _read(gc)
        return ""

    def _decl_body(decl):
        for c in decl.children:
            if c.type in ("object", "for_statement", "if_statement"):
                return c
        return None

    # ---- pass 1: declare every top-level symbol so references can resolve ----
    decls = [c for c in root.children if c.type.endswith("_declaration")]
    owners: list[tuple] = []
    for decl in decls:
        name, line = _decl_name(decl)
        if not name:
            continue
        if decl.type == "parameter_declaration":
            owner = b.add_node(name, f"param {name}", line, kind="param")
        elif decl.type == "variable_declaration":
            owner = b.add_node(name, f"var {name}", line, kind="var")
        elif decl.type == "resource_declaration":
            tlabel = _type_label(decl)
            # strip the @api-version suffix for the structured iac_type
            itype = tlabel.split("@", 1)[0] if tlabel else None
            is_existing = any(c.type == "existing" for c in decl.children)
            suffix = " (existing)" if is_existing else ""
            owner = b.add_node(name, f"resource {name} [{tlabel}]{suffix}", line,
                               kind="resource", iac_type=itype)
        elif decl.type == "module_declaration":
            modpath = _type_label(decl)
            owner = b.add_node(name, f"module {name}", line, kind="module",
                               iac_path=modpath or None)
            # cross-file deploy edge -> the target .bicep file node. The target id
            # is built from the path in the SAME form this extractor saw `path`
            # (not resolved), so it matches the target file's own pre-remap id and
            # extract()'s id_remap relativizes both consistently.
            if modpath and modpath.endswith(".bicep"):
                target = os.path.normpath(os.path.join(os.path.dirname(b.str_path), modpath))
                b.add_edge_to_id(owner, _make_id(str(target)), "deploys", line,
                                 target_file=str(target))
        elif decl.type == "output_declaration":
            owner = b.add_node(name, f"output {name}", line, kind="output")
        else:
            continue
        owners.append((decl, owner))

    # ---- pass 2: walk each declaration, emit references / depends_on / parent -
    def _walk(node, owner_nid: str, relation: str, loop_vars: frozenset) -> None:
        ntype = node.type

        # `for x in ...:` introduces a loop variable that is not a reference.
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
                                b.add_ref(owner_nid, _read(el), "depends_on",
                                          el.start_point[0] + 1, require_known=True)
                return
            if keyname == "parent":
                for c in node.children[1:]:
                    if c.type == "identifier":
                        b.add_ref(owner_nid, _read(c), "parent",
                                  c.start_point[0] + 1, require_known=True)
                return

        # a bare identifier, or the head (`.object`) of a member_expression
        if ntype == "identifier":
            nm = _read(node)
            if nm not in loop_vars and nm not in _BICEP_BUILTINS:
                b.add_ref(owner_nid, nm, relation, node.start_point[0] + 1,
                          require_known=True)
            return

        if ntype == "member_expression":
            obj = node.child_by_field_name("object")
            if obj is not None:
                _walk(obj, owner_nid, relation, loop_vars)
            return  # the `.property` chain is attribute access, not a reference

        for c in node.children:
            if c.is_named:
                _walk(c, owner_nid, relation, loop_vars)

    for decl, owner in owners:
        body = _decl_body(decl)
        if body is not None:
            _walk(body, owner, "references", frozenset())
        # param/output default & value expressions live after `=`, outside the body
        if decl.type in ("parameter_declaration", "output_declaration"):
            for c in decl.children:
                if c.is_named and c.type not in ("identifier", "type", "object",
                                                 "for_statement", "if_statement"):
                    _walk(c, owner, "references", frozenset())

    return b.result()


