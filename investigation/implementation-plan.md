# Implementation plan — Infrastructure-as-Code support for graphify

**Scope:** Bicep extraction, a generalized declarative-IaC extractor shared with
Terraform, and a two-step build that links across templates/stacks and into
application code. **Out of scope (tabled):** embeddings / vector retrieval.

**Target use case:** monorepos with complex infra — modules at multiple levels,
`dev`/`prod` environments, `app` vs `core`/`common` layers — plus the
application code that consumes the infra. Primary motivating project: an
Azure-services process-automation app.

---

## Design overview

### Where each piece hooks into the existing pipeline

```
detect() → extract() → build_from_json() → [NEW: link_iac()] → cluster() → analyze() → report()
              │                  │                  │
   per-file AST (parallel)   merge into        global post-build
   extract_bicep /           one nx.Graph      enrichment pass
   extract_terraform                           (the "2nd build step")
```

Two precedents in the current code make this low-risk:

1. **Per-language extractors** already follow a uniform `extract_X(path) ->
   {nodes, edges}` contract dispatched by suffix (`extract.py::_DISPATCH`). Bicep
   is just another entry.
2. **Cross-file resolution already exists** — `extract()` runs
   `_resolve_cross_file_imports` (Python), `_resolve_cross_file_java_imports`,
   and an all-language call-resolution pass after per-file extraction. The IaC
   linking pass is the same idea, but we run it as a **post-build graph pass**
   (`link_iac(G)`) because hub creation, cross-stack matching, and infra↔app
   linking are global operations cleaner to express on the merged `nx.Graph`
   than on raw dict fragments. This is the "make the build a 2-step process" the
   spec calls for.

### Module layout (new + changed)

| File | Change |
|---|---|
| `graphify/iac.py` *(new)* | `IaCGraphBuilder` helper (node/edge bookkeeping, scope handling, the standard IaC relation set, node annotations) shared by both extractors. |
| `graphify/extract.py` | Add `extract_bicep`; refactor `extract_terraform` onto `IaCGraphBuilder`; register suffixes. |
| `graphify/iac_link.py` *(new)* | `link_iac(G) -> G`: hub nodes, cross-file module resolution, monorepo scoping, infra↔app linking. Idempotent. |
| `graphify/build.py` | Call `link_iac(G)` at the end of `build_from_json` (gated: no-op when the graph has no IaC nodes), with an opt-out. |
| `graphify/detect.py` | Add `.bicep`, `.bicepparam` to `CODE_EXTENSIONS`. |
| `pyproject.toml` | New `bicep` extra; add to `all` + dev group. |
| `tests/` | `test_bicep.py`, `test_iac_link.py`, fixtures; keep `test_terraform.py` green unchanged. |

### IaC node annotations (the contract between extractor and link pass)

Both extractors annotate every IaC node so `link_iac` never re-parses. Added as
extra keys on the standard node dict (carried through `build_from_json`):

```python
{
  "id": ..., "label": ..., "file_type": "code", "source_file": ..., "source_location": ...,
  "iac_lang":  "bicep" | "terraform",
  "iac_kind":  "resource" | "module" | "param" | "var" | "output" | "data" | "provider" | "local",
  "iac_type":  "Microsoft.Storage/storageAccounts" | "aws_instance" | None,  # provider type, version stripped
  "iac_path":  "./modules/network.bicep" | "./modules/vpc" | None,           # module source, for resource=module
}
```

The standard relation set both extractors emit: `contains` (file→decl),
`references`, `depends_on`, `parent` (resource nesting), `deploys`
(module→target file).

---

## Stages

Each stage is one feature branch off `v8`, with its own commits and a green test
suite as its exit gate. Stages are ordered so each builds on a merged predecessor.

### Stage 0 — Bicep extractor + shared `IaCGraphBuilder`  *(branch: `feat/iac-bicep`)*

Foundation. The bicep extractor is already validated end-to-end by the prototype
in this directory (`extract_bicep_prototype.py`).

**Work**
- Add `graphify/iac.py` with `IaCGraphBuilder`: `add_node(name, label, line, *, kind, iac_type=None, iac_path=None)`, `add_edge(src, tgt, relation, line)`, dedup of nodes/edges, `contains` emission, scope-aware ID construction (`file` vs `directory`), `result()`.
- Implement `extract_bicep(path)` on top of it (port the prototype; emit annotations).
- Register `.bicep` + `.bicepparam` in `_DISPATCH` and `CODE_EXTENSIONS`.
- `pyproject.toml`: `bicep = ["tree-sitter-bicep>=1.1.0"]`; add to `all` + dev.
- Fix the prototype's one known gap: build the `deploys` target id the same way
  the target file's own node id is built (`_file_node_id` of the path relative to
  the corpus root) so the edge resolves instead of dangling.

**Tests** — `tests/test_bicep.py` (mirror `test_terraform.py`) + `tests/fixtures/sample.bicep`:
- all declaration kinds become nodes (param/var/resource/module/output);
- `references` edges incl. nested-object and `member_expression`-head refs;
- `depends_on` from `dependsOn` arrays; `parent` from `parent:`;
- string-interpolation refs (`${x}`) resolve;
- loop variables (`for i in …`) and builtins (`range`, `resourceGroup`) are **not** emitted;
- `existing` resources flagged; empty/comment-only files safe;
- `iac_lang`/`iac_kind`/`iac_type` annotations present and correct.

**Exit conditions**
- `pytest tests/test_bicep.py -q` green; **full suite still green**.
- `pip install graphifyy` (no extra) still imports; `extract_bicep` returns a
  friendly `error` string when `tree-sitter-bicep` is absent (no crash).
- `graphify extract` on a bicep folder produces the expected nodes/edges.

### Stage 1 — Refactor Terraform onto `IaCGraphBuilder`  *(branch: `feat/iac-generalize-tf`)*

Generalization with **behavior parity** — the existing extractor's output must
not change.

**Work**
- Reimplement `extract_terraform` using `IaCGraphBuilder` (directory scope) and
  emit the same annotations. HCL's grammar frontend (block/label parsing,
  `var.`/`data.` head resolution, `_TF_META_HEADS`) stays terraform-specific; only
  the bookkeeping/relation backend is shared.

**Tests**
- `tests/test_terraform.py` is **unchanged** and green (this is the parity proof).
- Add assertions for the new annotation keys on terraform nodes.

**Exit conditions**
- `test_terraform.py` passes with zero edits to its existing assertions.
- A before/after `graph.json` diff on a sample terraform dir shows only added
  annotation keys, no changed/removed nodes or edges.
- Net reduction in duplicated bookkeeping code between the two extractors.

### Stage 2 — Two-step build: resource-type hub nodes  *(branch: `feat/iac-type-hubs`)*

First feature of `link_iac`. Turns every distinct `iac_type` into one shared
concept node so "everything that touches Key Vault / Storage" becomes a god-node.

**Work**
- `graphify/iac_link.py::link_iac(G)`: for each distinct non-null `iac_type`,
  create one `type=concept` hub node (id keyed by the type string, stable) and an
  `instance_of` edge from each resource instance to it. Idempotent (re-running on
  an already-linked graph is a no-op — guard by hub-id existence).
- Call from `build_from_json` end, gated `if any iac node`, with a
  `link_iac=True` kwarg / `GRAPHIFY_NO_IAC_LINK` opt-out.

**Tests** — `tests/test_iac_link.py`:
- N storage accounts across files → exactly one `Microsoft.Storage/storageAccounts` hub with N `instance_of` edges;
- running `link_iac` twice yields an identical graph (idempotency);
- a graph with no IaC nodes is returned untouched;
- hubs survive `cluster()` and pull their instances into one community.

**Exit conditions**
- Idempotency test passes; non-IaC graphs byte-identical before/after.
- Full suite green; the skill scripts (which call `build_from_json`) get hubs
  automatically with no edit.

### Stage 3 — Two-step build: cross-file module resolution + monorepo scoping  *(branch: `feat/iac-monorepo-scope`)*

Make the graph understand a real monorepo layout.

**Work**
- In `link_iac`: resolve `deploys`/module `iac_path` references to the actual
  target file node now that all files are in the graph (relative-path join from
  the module's `source_file` dir, normalized to the corpus-root-relative id).
- Derive and attach a **layer/environment** attribute to each IaC node from its
  path, with a small, documented, override-able rule set:
  - environment: path segment matching `dev|test|stage|staging|prod|production` (and `environments/<x>`);
  - layer: `module(s)/…` → `module`; `core`/`common`/`shared` → `core`; `app`/`apps`/`services` → `app`; else `root`.
- These attributes feed clustering/queries (e.g. "prod storage" vs "dev storage").

**Tests**
- multi-directory fixture (`environments/prod/main.bicep` referencing
  `modules/network/main.bicep`) → the `deploys` edge resolves to the real module
  file node (not dangling);
- nodes carry correct `iac_env` / `iac_layer`;
- a `module` reused by `dev` and `prod` is one file node referenced by both
  (correct fan-in), and instances stay distinguishable by `iac_env`.

**Exit conditions**
- Cross-file deploy edges resolve in the multi-dir fixture (asserted present in
  the built graph, à la `test_terraform.py::test_cross_file_references_resolve_after_merge`).
- Path→env/layer rules covered by unit tests incl. edge cases (no env segment,
  nested modules).

### Stage 4 — Infra↔app linking  *(branch: `feat/iac-app-linking`)*

The highest-value connection for the target use case: link infra resources to the
application code that consumes them.

**Work**
- In `link_iac`, after hubs/scoping: build an index of IaC "consumable" tokens
  (resource symbolic names, `output` names, and `iac_type` leaf names) and scan
  **application-code** node labels / rationale text / config-doc nodes for matches.
  Emit `INFERRED` edges (`configures` / `consumed_by`) with a confidence score per
  the existing rubric (0.65 naming-only … 0.85 naming+context).
- Precision guardrails: skip short/common tokens (reuse the IDF idea — a token
  matching many app nodes is noise), require min length, and tag everything
  `INFERRED`/`AMBIGUOUS` so the report flags it for review (never `EXTRACTED`).

**Tests**
- fixture: a bicep `output storageAccountName` + an app file referencing
  `storageAccountName` → one `INFERRED` `consumed_by` edge;
- a common token (e.g. `name`, `id`) does **not** spawn edges;
- no infra nodes ⇒ no app-link edges; deterministic output across runs.

**Exit conditions**
- Precision check on a realistic fixture: zero edges from common tokens; expected
  edges present; all such edges `INFERRED`/`AMBIGUOUS`, never `EXTRACTED`.
- Full suite green; a `worked/` example on a real Azure bicep+app repo reviewed
  for quality (per CONTRIBUTING).

---

## Cross-cutting exit criteria (every stage)

1. `uv run pytest tests/ -q` green (full suite, not just new tests).
2. Base `pip install graphifyy` (no extras) imports and runs on a non-IaC corpus
   with byte-identical output to `main` — the IaC path is fully opt-in/no-op.
3. No new hard dependency in the base install (bicep grammar stays an extra).
4. Each stage is one squashable feature branch with conventional-commit messages
   (`feat:` / `test:` / `refactor:`), mergeable independently.
5. Docs updated in the stage that changes behavior (README file-table, the new
   `bicep` extra row, ARCHITECTURE pipeline diagram for `link_iac`).

## Open decisions (defaults chosen; flag if you disagree)

- **`link_iac` default-on** inside `build_from_json`, no-op without IaC nodes,
  `GRAPHIFY_NO_IAC_LINK=1` to disable. (Alternative: opt-in flag — but default-on
  makes the skill flow benefit with zero edits.)
- **Hub node `file_type`** = `concept` (reuses the existing legacy/None bucket the
  validator already accepts) rather than a new file_type.
- **Env/layer rules** are heuristic and override-able via `.graphify` config
  later; Stage 3 ships sensible defaults only.
