# Graphify investigation — Bicep support & improvement opportunities

**Date:** 2026-06-22
**Author:** investigation for paavo.reinikka
**Verdict (bicep):** ✅ **Viable and low-risk.** A tree-sitter Bicep grammar ships
on PyPI with prebuilt wheels (incl. Windows), and a working extractor prototype
already produces correct nodes and edges against real Bicep — see
[`extract_bicep_prototype.py`](./extract_bicep_prototype.py) and the test output
captured below.

---

## 1. How graphify works (the basic principles)

Graphify turns a folder of code + documents into a **knowledge graph** you query
instead of grepping. It is a Python library (`graphify/`) orchestrated by an
AI-assistant "skill". The core pipeline is a straight line of pure functions that
pass plain dicts / NetworkX graphs — no shared state (see `ARCHITECTURE.md`):

```
detect() → extract() → build_graph() → cluster() → analyze() → report() → export()
```

| Stage | Module | What it does |
|-------|--------|--------------|
| detect | `detect.py` | Walk the folder, classify each file (code / doc / paper / image / video) by extension, honor `.gitignore` + `.graphifyignore`. |
| extract | `extract.py` | Turn each file into `{nodes, edges}`. **This is where language support lives.** |
| build | `build.py` | Merge all per-file extractions into one `nx.Graph`, normalize node IDs, drop dangling edges, merge ghost duplicates. |
| cluster | `cluster.py` | Group nodes into communities with the **Leiden** algorithm (edge-density clustering). |
| analyze | `analyze.py` | Find "god nodes" (high-degree hubs), surprising cross-module links, suggested questions. |
| report / export | `report.py`, `export.py` | Emit `GRAPH_REPORT.md`, `graph.json`, `graph.html`, plus optional SVG / GraphML / Neo4j / Obsidian / wiki. |

### Three extraction passes

1. **Code (free, fully local).** Tree-sitter parses each source file into an AST;
   graphify walks it to emit nodes (classes, functions, resources…) and edges
   (`calls`, `imports`, `references`…). **No LLM, no network.** A code-only corpus
   costs zero tokens and runs offline. *This is the pass bicep slots into.*
2. **Video / audio (local).** `faster-whisper` transcribes media.
3. **Docs / PDFs / images (LLM).** Only non-code files are sent to an LLM
   subagent for semantic extraction.

### How nodes and edges are modeled

Every extractor returns the same schema (`validate.py` enforces it):

```json
{
  "nodes": [{"id": "...", "label": "...", "file_type": "code",
             "source_file": "...", "source_location": "L42"}],
  "edges": [{"source": "id_a", "target": "id_b", "relation": "references",
             "confidence": "EXTRACTED|INFERRED|AMBIGUOUS", "weight": 1.0}]
}
```

- **Node IDs** are produced by one canonical normalizer (`ids.py::make_id`) shared
  by the AST extractor, the LLM, and the builder, so the three never drift and an
  edge from file A resolves to a node defined in file B.
- **Confidence** is `EXTRACTED` (literally in the source — imports, calls, AST
  edges = always 1.0), `INFERRED` (a reasoned guess with a 0.55–0.95 score), or
  `AMBIGUOUS` (flagged for human review). AST extractors emit `EXTRACTED`.
- A **SHA-256 cache** (`graphify-out/cache/`) fingerprints every file so re-runs
  skip unchanged files.

### How querying works (relevant to the embeddings question — see §5)

`graphify query` / the MCP server do **lexical retrieval**: the question is split
into terms, each term is scored against node **labels** with TF-IDF weighting
(`serve.py::_compute_idf`, `_score_nodes`), the top nodes are selected, and the
result is expanded by walking graph neighbors (BFS/DFS). **There is no embedding
model and no vector store anywhere in the codebase.** Semantic grouping comes
entirely from (a) graph structure via Leiden clustering and (b)
`semantically_similar_to` edges that the LLM emits during doc extraction.

---

## 2. Why bicep is a natural fit

Bicep is Azure's infrastructure-as-code DSL — the same problem domain as
**Terraform/HCL, which graphify already supports** (`extract_terraform`, the
`[terraform]` extra, `tests/test_terraform.py`). Bicep maps onto graphify's model
even more cleanly than HCL:

| Bicep construct | Graph node | Graph edges |
|---|---|---|
| `param x string` | `param x` | — |
| `var x = ...` | `var x` | `references` → symbols it uses |
| `resource r 'Type@api' = {...}` | `resource r [Type@api]` | `references`, `depends_on`, `parent` |
| `module m './other.bicep' = {...}` | `module m` | `references`, `deploys` → the target `.bicep` file |
| `output o = ...` | `output o` | `references` |

Bicep is **file-scoped** (one `.bicep` file = one deployment template, symbol
names unique within it), so node IDs are scoped by file path — simpler than
Terraform's directory scoping. Cross-file links happen only through `module`
path references, which become a `deploys` edge to the referenced file's node.

---

## 3. The tree-sitter grammar is real and installable

```
pip install tree-sitter-bicep        # v1.1.0, prebuilt wheels for Py3.9+
```

Verified live in this investigation: the wheel installed on **Windows / Python
3.12** with no compiler, and the Python binding follows the standard pattern
graphify already uses for every other grammar:

```python
import tree_sitter_bicep as tsb
from tree_sitter import Language, Parser
parser = Parser(Language(tsb.language()))
```

**Node types discovered empirically** (by parsing the sample files — see
`_introspect.py`):

- root: `infrastructure`
- `parameter_declaration` → `param <identifier> <type> [= <expr>]`
- `variable_declaration` → `var <identifier> = <expr>`
- `resource_declaration` → `resource <identifier> '<Type@api>' [existing] = <object | for_statement>`
- `module_declaration` → `module <identifier> '<./path.bicep>' = <object>`
- `output_declaration` → `output <identifier> <type> = <expr>`
- references between symbols appear as **bare `identifier` nodes** and as the
  **head of a `member_expression`** (`storage.id` → object=`storage`, the
  `.id` chain is attribute access);
- `dependsOn: [ <id> ... ]` is an `object_property` whose value is an `array`;
- `parent: <id>` nests a resource;
- string interpolation `'nsg-${env}'` exposes `env` as an `identifier` inside an
  `interpolation` node;
- `for_statement` has an `.initializer` field giving the loop variable name (must
  be excluded from references).

---

## 4. Implementation plan (drop-in, ~5 steps)

This follows the documented "Adding a new language extractor" recipe in
`ARCHITECTURE.md` exactly. A **working, tested prototype** of step 1 already
exists at [`extract_bicep_prototype.py`](./extract_bicep_prototype.py).

1. **Add `extract_bicep(path)` to `graphify/extract.py`.** Copy the prototype;
   change `_make_id` (local mirror) to the module-level `_make_id`. It mirrors
   `extract_terraform` in structure (parse → pass 1 declare symbols → pass 2 walk
   bodies for references → return `{nodes, edges}`).
2. **Register the suffix** in the `_DISPATCH` table:
   ```python
   ".bicep": extract_bicep,
   ".bicepparam": extract_bicep,   # optional: Bicep parameter files
   ```
3. **Add `.bicep` (and `.bicepparam`) to `CODE_EXTENSIONS`** in `detect.py`
   (line 29). `watch.py::_WATCHED_EXTENSIONS` derives from this set automatically.
4. **Add the dependency** to `pyproject.toml`: a new extra
   `bicep = ["tree-sitter-bicep>=1.1.0"]`, add it to the `all` extra, and to the
   dev group. Keep it optional (like `terraform`/`dm`) so the base install stays
   slim — `extract_bicep` already returns a friendly `error` string when the
   grammar isn't installed.
5. **Add a fixture + tests**: `tests/fixtures/sample.bicep` and
   `tests/test_bicep.py` mirroring `tests/test_terraform.py` (assert all block
   types become nodes; assert `references` / `depends_on` / `parent` / `contains`
   edges; assert loop variables and builtins are *not* emitted; assert
   empty/comment-only files are safe).

Also update the README "What files it handles" table and the grammar count.

### Prototype test output (proof it works)

Run against the two sample files in this directory:

```
===== sample.bicep =====  9 nodes, 16 edges
  resource storage --references--> param storageName          (nested object value)
  resource storage --references--> var storageSku             (resolved through nested sku{})
  resource blob    --parent-->     resource storage           (resource nesting)
  module networking --deploys-->   .../modules/network.bicep  (cross-file)
  module networking --references-> param location
  output storageId --references--> resource storage           (member_expression head, .id ignored)

===== sample2.bicep =====  6 nodes, 10 edges
  resource existingVnet ... (existing)                         (`existing` keyword flagged)
  resource nsg --references--> param env                       (string interpolation ${env})
  resource nic --depends_on--> resource nsg                    (dependsOn array)
  resource nic --depends_on--> resource existingVnet
  resource vms --depends_on--> resource nic                    (inside a for-loop body)
  # loop variable `i` and builtin range() correctly NOT emitted as references
```

Every edge type graphify cares about is produced, and the two classic
false-positive traps (loop variables, builtin functions) are avoided.

### Known refinements before merging (small)

- **`deploys` edge target id.** The prototype resolves the module path to an
  absolute path. The final version must build the target id the *same way*
  graphify builds the target file's own node id (`_make_id(str(path))` where
  `path` is the path as `detect.collect_files` yields it — typically relative to
  the corpus root). If the two forms differ, `build.py` will drop the edge as
  dangling. Easiest: resolve relative to the corpus root, matching how other
  cross-file edges are anchored. (Terraform sidesteps this by using
  directory-scoped *symbol* ids and not emitting a file→file edge at all.)
- **`@description('...')` decorators** could optionally become `rationale` nodes
  linked to the declaration they annotate — this is exactly graphify's "the why"
  feature for code comments, and bicep decorators are first-class metadata.
- **Type strings** (`Microsoft.Storage/storageAccounts@2023-01-01`) are kept in
  the label; consider also emitting a shared "resource type" node so all storage
  accounts across files cluster together (a nice god-node).

---

## 5. Other improvement opportunities

### 5a. Embeddings / semantic retrieval (the user's question)

**Current state:** graphify is deliberately a *no-embeddings* GraphRAG. Its
design doc states this explicitly ("No embeddings needed… the graph structure is
the similarity signal"). Retrieval is **lexical TF-IDF over node labels** plus
graph traversal; clustering is **Leiden over edge density**.

**Where this is fine:** code-structure queries where the user's words overlap the
symbol names ("what calls `RateLimiter`?"). Fast, free, offline, deterministic.

**Where it falls short:** natural-language questions whose vocabulary doesn't
appear in any label. "How do we keep data when a VM is deleted?" will not match a
node labeled `resource disk [Microsoft.Compute/disks]` because no query token is
a substring of the label. Lexical recall has a hard ceiling here.

**Recommended improvement — optional embedding-backed retrieval, additive not
replacing:**
- Add an opt-in `[embeddings]` extra (e.g. a local `sentence-transformers` model,
  or reuse the already-configured LLM backend's embedding endpoint). Embed each
  node's label + a short context string at build time; store vectors alongside
  `graph.json` (or in a small sidecar `embeddings.npy` + id index).
- In `serve.py::_score_nodes`, blend the existing lexical score with cosine
  similarity to the query embedding (hybrid retrieval). Keep lexical as the
  default so offline / no-key usage is unchanged.
- Bonus: embeddings would also let `analyze.py` propose `semantically_similar_to`
  edges for **code** (today only the LLM adds those, and only for docs), improving
  cross-module "surprising connection" discovery.

This is the single highest-leverage robustness improvement and is cleanly
isolated to two modules (build-time embedding, query-time blending).

### 5b. Generalize the "infra-as-code" extractor pattern

Terraform and Bicep share a shape (declarations + references + depends_on). A
small shared helper (`_extract_declarative_iac`) parameterized by grammar +
node-type map would reduce duplication and make the next IaC language (Pulumi
YAML, ARM JSON, CloudFormation, Kubernetes manifests) cheap. Not required for
bicep, but worth it if you add more infra formats.

### 5c. Cross-template / cross-stack linking

Bicep `module` references and Terraform `module`/`data` references both point
across files. A post-build pass that links IaC resources to the **application
code** that consumes them (e.g. a storage account name appearing in an app's
config) would make the graph span infra ↔ app — the most valuable connection for
your use case (you write both). This is a natural fit for the embedding work in
5a (match by semantic similarity, tag `INFERRED`).

### 5d. Lower-effort robustness wins

- **Resource-type hub nodes** (5 above) — turns every `Microsoft.*/*` type into a
  shared concept node, surfacing "everything that touches Key Vault".
- **`.bicepparam` support** — the newer typed parameter-file format; same grammar
  handles it, so it's nearly free.
- **A worked example** under `worked/` on a real Bicep repo, per the contributing
  guide, to validate output quality end-to-end.

---

## 6. Files in this investigation directory

| File | Purpose |
|---|---|
| `findings.md` | This report. |
| `extract_bicep_prototype.py` | Working, tested bicep extractor (drop-in for `extract.py`). |
| `_introspect.py` | Dumps the tree-sitter parse tree — how the node types were discovered. |
| `sample.bicep`, `sample2.bicep` | Test inputs covering params/vars/resources/modules/outputs, `existing`, `dependsOn`, `parent`, interpolation, and `for` loops. |

To reproduce:

```powershell
uv venv .scratch-venv
uv pip install --python .scratch-venv tree-sitter-bicep "tree-sitter>=0.23"
.scratch-venv/Scripts/python.exe investigation/extract_bicep_prototype.py
```
