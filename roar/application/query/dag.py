"""Application orchestration for the local DAG query."""

from __future__ import annotations

import json

from ...core.models.dag import DagVisualization
from ...db.context import create_database_context
from ...presenters.dag_data_builder import DagDataBuilder
from .requests import DagQueryRequest


def build_dag_visualization(request: DagQueryRequest) -> DagVisualization:
    """Build a typed DAG visualization for the active session."""
    from ...core.models.dag import (
        DagArtifactInfo,
        DagArtifactState,
        DagNodeInfo,
        DagNodeMetrics,
        DagNodeState,
    )

    with create_database_context(request.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            raise ValueError("No active session. Run 'roar init' or 'roar run' first.")

        builder = DagDataBuilder(db_ctx, int(session["id"]))
        dag_data = builder.build(
            expanded=request.expanded,
            show_artifacts=request.show_artifacts,
            stale_only=request.stale_only,
        )

    nodes = [
        DagNodeInfo(
            step_number=node["step_number"],
            job_id=node["job_id"],
            job_uid=node["job_uid"],
            command=node["command"],
            step_name=node["step_name"],
            state=DagNodeState(node["state"]),
            is_build=node["is_build"],
            exit_code=node["exit_code"],
            metrics=DagNodeMetrics(**node["metrics"]),
            dependencies=node["dependencies"],
            labels=node.get("labels", {}),
        )
        for node in dag_data["nodes"]
    ]
    artifacts = [
        DagArtifactInfo(
            path=artifact["path"],
            hash=artifact["hash"],
            is_stale=artifact["is_stale"],
            producer_step=artifact["producer_step"],
            state=DagArtifactState(artifact["state"]),
            artifact_id=artifact["artifact_id"],
            consumer_steps=artifact["consumer_steps"],
            is_terminal=artifact["is_terminal"],
            superseded_by=artifact["superseded_by"],
            labels=artifact.get("labels", {}),
        )
        for artifact in dag_data["artifacts"]
    ]
    return DagVisualization(
        nodes=nodes,
        artifacts=artifacts,
        stale_count=dag_data["stale_count"],
        total_steps=dag_data["total_steps"],
        is_expanded=dag_data["is_expanded"],
        session_id=dag_data["session_id"],
        stale_artifact_count=dag_data["stale_artifact_count"],
        superseded_artifact_count=dag_data["superseded_artifact_count"],
        labels=dag_data.get("labels", {}),
    )


def render_dag(request: DagQueryRequest) -> str:
    """Render the current session DAG."""
    from ...presenters.dag_renderer import DagRenderer

    dag_viz = build_dag_visualization(request)
    renderer = DagRenderer(use_color=request.use_color)
    if request.output_json:
        return json.dumps(renderer.render_json(dag_viz), indent=2)
    return renderer.render(dag_viz)
