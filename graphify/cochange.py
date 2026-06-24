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


def graphmine_available() -> bool:
    return shutil.which("graphmine") is not None


def enrich_with_cochange(
    repo: Path,
    graph_path: Path,
    out_dir: Path,
    *,
    correction: str = "bh",
    alpha: float = 0.05,
    subsystem_depth: int = 1,
    include_deleted: bool = False,
    extra_args: list[str] | None = None,
) -> int | None:
    """Run ``graphmine cochange`` to augment *graph_path*. Returns the subprocess
    exit code, or ``None`` if graphmine is not installed."""
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
        "--correction", correction,
        "--alpha", str(alpha),
        "--subsystem-depth", str(subsystem_depth),
    ]
    if include_deleted:
        cmd.append("--include-deleted")
    if extra_args:
        cmd += extra_args
    # encoding="utf-8": graphmine's digest contains non-ASCII (⇔, ·) that would
    # crash a cp1252 pipe on Windows.
    proc = subprocess.run(cmd, encoding="utf-8")
    if proc.returncode == 0:
        print(f"[graphify] co-change layer written under {out_dir} "
              f"(cochange.md + cochange.graphify.json — graph.json left untouched).")
    return proc.returncode
