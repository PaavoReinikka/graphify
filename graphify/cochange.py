"""Optional co-change enrichment via the standalone `graphmine` tool.

graphmine (https://github.com/PaavoReinikka/graphmine) mines statistically
significant *co-change* couplings from a repo's git history and can emit them as
additive, typed ``co_changes_with`` edges over a graphify ``graph.json``. graphify
shells out to the ``graphmine`` CLI when it is installed — there is **no hard
dependency**, and graphmine's Kingfisher/Rust stack stays entirely out of
graphify. If ``graphmine`` is not on PATH this is a friendly no-op.

The result is additive: graphmine writes ``<out>/cochange.graphify.json`` (a copy
of the graph with the extra edges) plus a standalone report — the original
``graph.json`` is left untouched, so the temporal-coupling layer stays optional
and filterable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Marker-delimited section appended to assistant instruction files (CLAUDE.md,
# AGENTS.md, ...) by `graphify cochange --update-instructions`. Its own `## `
# heading so graphify's _replace_or_append_section treats it as a sibling of the
# base `## graphify` block (idempotent update, removable, never rewrites the base
# block). Kept inline (not an always_on/ file) so it stays out of skillgen's
# always-on-roundtrip validation — this is a fork-local addition.
COCHANGE_RELATION = "co_changes_with"
_COCHANGE_MARKER = "## graphify: co-change"
_COCHANGE_INSTRUCTION = """\
## graphify: co-change

This project also has a statistical **co-change** layer mined from git history
(via graphmine): `graphify-out/cochange.md` is a clustered digest and
`graphify-out/cochange.graphify.json` adds `co_changes_with` edges (a STATISTICAL
confidence tier whose score is a raw Fisher p-value; smaller = stronger).

Rules:
- For impact / blast-radius / refactoring questions, consult the co-change layer:
  files that change together are coupled in ways static structure (imports,
  calls) cannot show — e.g. generated-together files, a config and its consumer,
  a SQL table and its triggers.
- When editing or assessing a file, check its co-change partners in
  `graphify-out/cochange.md` and weigh them in "what else might need to change".
- `graphify affected <file>` (or the MCP `affected` tool) gives a blast radius
  that includes co-change partners (hits tagged `[co_changes_with]`).
- These edges are statistical hints, not guarantees — weight them by the p-value.
"""

# Instruction files that carry graphify's `## graphify` always-on block, relative
# to the project root. We only touch ones that already opted into graphify.
_INSTRUCTION_CANDIDATES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "CODEBUDDY.md",
    ".github/copilot-instructions.md",
)
_GRAPHIFY_BLOCK_MARKER = "## graphify"


def graphmine_available() -> bool:
    return shutil.which("graphmine") is not None


def enrich_with_cochange(
    repo: Path,
    graph_path: Path,
    out_dir: Path,
    *,
    extra_args: list[str] | None = None,
) -> int | None:
    """Run ``graphmine cochange`` to augment *graph_path*. Returns the subprocess
    exit code, or ``None`` if graphmine is not installed.

    graphify only supplies what it owns (the repo, the graph to augment and the
    output dir); every mining option (``--alpha``, ``--subsystem-depth``,
    ``--significance``, ``--min-freq``, ...) is forwarded verbatim in
    *extra_args*, so graphmine's own defaults and flag set stay authoritative
    and the two tools cannot drift apart."""
    exe = shutil.which("graphmine")
    if not exe:
        print(
            "[graphify] co-change enrichment needs the 'graphmine' tool, which is "
            "not on PATH.\n           Install it from "
            "https://github.com/PaavoReinikka/graphmine and re-run.",
            file=sys.stderr,
        )
        return None

    cmd = [
        exe, "cochange", str(repo),
        "--graphify-graph", str(graph_path),
        "--out", str(out_dir),
    ]
    if extra_args:
        cmd += extra_args
    # encoding="utf-8": graphmine's digest contains non-ASCII (⇔, ·) that would
    # crash a cp1252 pipe on Windows.
    proc = subprocess.run(cmd, encoding="utf-8")
    if proc.returncode == 0:
        print(f"[graphify] co-change layer written under {out_dir} "
              f"(cochange.md + cochange.graphify.json — graph.json left untouched).")
    return proc.returncode


_USAGE = """Usage: graphify cochange [repo] [--graph PATH] [--update-instructions] [graphmine options...]
  Augment graph.json with co_changes_with edges mined from git history
  (requires the optional 'graphmine' tool on PATH).
  --graph PATH           graph.json to augment (default graphify-out/graph.json)
  --update-instructions  add a co-change section to graphify-configured
                         CLAUDE.md/AGENTS.md/etc.
  Any other flag is forwarded to `graphmine cochange` (e.g. --alpha,
  --subsystem-depth, --significance tarone, --min-freq, --exclude,
  --include-deleted); see `graphmine cochange --help`. Give [repo] before
  any forwarded flags."""


def affected_relations(graph, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Default relation set for ``graphify affected``: *defaults*, plus
    ``co_changes_with`` when *graph* carries co-change edges (a
    ``cochange.graphify.json``), so blast radius includes files that change
    together. A plain graph.json is unaffected. Hits stay labelled
    ``[co_changes_with]``, and an explicit ``--relation`` still replaces the set."""
    if any(d.get("relation") == COCHANGE_RELATION for _, _, d in graph.edges(data=True)):
        return (*defaults, COCHANGE_RELATION)
    return defaults


COCHANGE_GRAPH_NAME = "cochange.graphify.json"
_overlay_cache: dict[str, tuple[tuple[int, int], list[tuple[str, str, dict]]]] = {}


def cochange_overlay_edges(graph_path: "str | Path") -> list[tuple[str, str, dict]]:
    """``co_changes_with`` edges from the ``cochange.graphify.json`` next to
    *graph_path*, or ``[]`` when there is none (or *graph_path* is that file).
    Cached on the sidecar's mtime/size, so a re-run of `graphify cochange` is
    picked up without restarting a long-lived MCP server."""
    import json

    gp = Path(graph_path)
    side = gp.parent / COCHANGE_GRAPH_NAME
    if gp.name == COCHANGE_GRAPH_NAME or not side.is_file():
        return []
    st = side.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _overlay_cache.get(str(side))
    if hit and hit[0] == key:
        return hit[1]
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    links = data.get("links") if isinstance(data.get("links"), list) else data.get("edges", [])
    edges = [
        (str(e["source"]), str(e["target"]), dict(e))
        for e in links
        if isinstance(e, dict) and e.get("relation") == COCHANGE_RELATION
        and "source" in e and "target" in e
    ]
    _overlay_cache[str(side)] = (key, edges)
    return edges


def with_cochange_overlay(G, graph_path: "str | Path"):
    """*G* for blast-radius queries: unchanged if it already carries co-change
    edges or no sidecar exists, else a copy with the sidecar's edges added
    between nodes that still exist (a stale sidecar never invents nodes)."""
    if any(d.get("relation") == COCHANGE_RELATION for _, _, d in G.edges(data=True)):
        return G
    overlay = [(u, v, d) for u, v, d in cochange_overlay_edges(graph_path)
               if u in G and v in G and not G.has_edge(u, v)]
    if not overlay:
        return G
    H = G.copy()
    for u, v, d in overlay:
        attrs = {k: v2 for k, v2 in d.items() if k not in ("source", "target")}
        H.add_edge(u, v, **attrs)
    return H


def run_cli(argv: list[str]) -> None:
    """Entry point for ``graphify cochange`` (*argv* excludes the command)."""
    repo, graph_arg, repo_set, update_instr = ".", None, False, False
    passthrough: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(_USAGE)
            return
        if a == "--graph" and i + 1 < len(argv):
            graph_arg = argv[i + 1]; i += 2; continue
        if a == "--update-instructions":
            update_instr = True; i += 1; continue
        # [repo] must precede forwarded flags: once one is seen, a bare token
        # may be that flag's value, so it is forwarded rather than guessed at.
        if not a.startswith("-") and not repo_set and not passthrough:
            repo = a; repo_set = True; i += 1; continue
        # anything else (graphmine flag or its value) -> forward verbatim
        passthrough.append(a); i += 1

    from graphify.cli import _default_graph_path
    graph_path = Path(graph_arg or _default_graph_path())
    if not graph_path.exists():
        print(f"error: graph not found at {graph_path}; build it first with "
              f"`graphify extract`", file=sys.stderr)
        sys.exit(1)
    rc = enrich_with_cochange(Path(repo), graph_path, graph_path.parent,
                              extra_args=passthrough or None)
    # Only touch instruction files when the co-change layer was actually
    # produced (graphmine present and succeeded) — opt-in via the flag.
    if update_instr and rc == 0:
        update_instructions(Path(repo))
    if rc:
        sys.exit(rc)


def update_instructions(project_dir: Path) -> list[Path]:
    """Add the co-change awareness section to assistant instruction files that
    already carry graphify's `## graphify` block. Idempotent; returns the files
    updated. Only touches files that opted into graphify (have the base block),
    so it never creates instructions out of nowhere."""
    from graphify.install import _replace_or_append_section

    updated: list[Path] = []
    for rel in _INSTRUCTION_CANDIDATES:
        path = project_dir / rel
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if _GRAPHIFY_BLOCK_MARKER not in content:
            continue  # file isn't graphify-configured; leave it alone
        new_content = _replace_or_append_section(
            content, _COCHANGE_MARKER, _COCHANGE_INSTRUCTION
        )
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            updated.append(path)
    if updated:
        print("[graphify] co-change guidance added to: "
              + ", ".join(str(p) for p in updated))
    else:
        print("[graphify] no graphify-configured instruction files found to update "
              "(run `graphify <platform> install` first).", file=sys.stderr)
    return updated
