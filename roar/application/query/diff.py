"""Application orchestration for the roar diff command."""

from __future__ import annotations

from ...db.query_context import create_query_database_context
from .diff_engine import compare_lineage_graphs
from .diff_graph import build_diff_graph
from .diff_refs import DiffError
from .requests import DiffQueryRequest
from .results import DiffResult


def build_diff_summary(request: DiffQueryRequest) -> DiffResult:
    """Build a diff summary between two supported lineage references."""
    max_depth = request.depth if request.depth is not None else 10

    with create_query_database_context(request.roar_dir) as db_ctx:
        graph_a = build_diff_graph(db_ctx, request.ref_a, request.cwd, max_depth)
        graph_b = build_diff_graph(db_ctx, request.ref_b, request.cwd, max_depth)

    comparison = compare_lineage_graphs(graph_a, graph_b)
    targets_identical = (
        graph_a.target_hash is not None
        and graph_b.target_hash is not None
        and graph_a.target_hash == graph_b.target_hash
    )

    return DiffResult(
        ref_a=request.ref_a,
        ref_b=request.ref_b,
        target_a_hash=graph_a.target_hash,
        target_b_hash=graph_b.target_hash,
        target_a_path=graph_a.target_path,
        target_b_path=graph_b.target_path,
        targets_identical=targets_identical,
        diffs=comparison.diffs,
        matched_jobs=comparison.matched_jobs,
        only_in_a=comparison.only_in_a,
        only_in_b=comparison.only_in_b,
        root_cause=comparison.root_cause,
    )


def build_diff(request: DiffQueryRequest) -> DiffResult:
    """Backward-compatible alias for the diff summary builder."""
    return build_diff_summary(request)


def render_diff(request: DiffQueryRequest) -> str:
    """Build and render a diff between two references."""
    from ...presenters.diff_renderer import DiffRenderer

    result = build_diff_summary(request)
    renderer = DiffRenderer()

    if request.output_json:
        return renderer.render_json(result)
    return renderer.render_text(result, format=request.format)


__all__ = [
    "DiffError",
    "build_diff",
    "build_diff_summary",
    "render_diff",
]
