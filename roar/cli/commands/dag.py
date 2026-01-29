"""
Native Click implementation of the dag command.

Usage: roar dag [options]
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from ...db.context import create_database_context
from ..context import RoarContext
from ..decorators import require_init


def _get_dag_data(
    db_ctx,
    session_id: int,
    expanded: bool = False,
    show_artifacts: bool = False,
    stale_only: bool = False,
) -> dict[str, Any]:
    """
    Build DAG visualization data from session.

    Args:
        db_ctx: Database context
        session_id: Session ID
        expanded: Whether to include superseded job executions
        show_artifacts: Whether to include intermediate artifacts
        stale_only: Whether to filter to only stale steps and artifacts

    Returns:
        Dictionary with DAG visualization data
    """
    # Get all steps in the session
    steps = db_ctx.sessions.get_steps(session_id)
    if not steps:
        return {
            "nodes": [],
            "artifacts": [],
            "stale_count": 0,
            "total_steps": 0,
            "is_expanded": expanded,
            "session_id": session_id,
            "stale_artifact_count": 0,
            "superseded_artifact_count": 0,
        }

    # Get stale steps
    stale_steps = set(db_ctx.session_service.get_stale_steps(session_id))

    # Group steps by step_number to find latest of each
    steps_by_number: dict[int, list[dict]] = {}
    for step in steps:
        num = step["step_number"]
        if num not in steps_by_number:
            steps_by_number[num] = []
        steps_by_number[num].append(step)

    # Sort each group by timestamp
    for num in steps_by_number:
        steps_by_number[num].sort(key=lambda s: s["timestamp"])

    # Determine which steps to include
    if expanded:
        # Include all executions
        steps_to_show = steps
    else:
        # Only latest execution of each step
        steps_to_show = [steps_by_number[num][-1] for num in sorted(steps_by_number.keys())]

    # Build node data with inputs/outputs/consumed metrics
    nodes = []
    latest_by_step: dict[int, dict] = {num: steps_by_number[num][-1] for num in steps_by_number}

    # Collect all artifacts for producer/consumer mapping
    # artifact_id -> {path, hash, producer_step, job_id}
    all_artifacts: dict[str, dict] = {}
    # path -> list of artifact_ids (for superseded detection)
    artifacts_by_path: dict[str, list[str]] = {}
    # artifact_id -> list of consumer step numbers
    artifact_consumers: dict[str, list[int]] = {}

    # First pass: collect all outputs from all steps (for latest runs)
    for num, step in latest_by_step.items():
        outputs = db_ctx.jobs.get_outputs(step["id"], db_ctx.artifacts)
        for out in outputs:
            path = out.get("path") or out.get("first_seen_path")
            if not path:
                continue
            artifact_id = str(out.get("artifact_id", ""))
            artifact_hash = None
            if out.get("hashes"):
                for h in out["hashes"]:
                    if h.get("algorithm") == "blake3":
                        artifact_hash = h.get("digest")
                        break
                if not artifact_hash and out["hashes"]:
                    artifact_hash = out["hashes"][0].get("digest")

            all_artifacts[artifact_id] = {
                "path": path,
                "hash": artifact_hash,
                "producer_step": num,
                "job_id": step["id"],
                "artifact_id": artifact_id,
            }

            if path not in artifacts_by_path:
                artifacts_by_path[path] = []
            if artifact_id not in artifacts_by_path[path]:
                artifacts_by_path[path].append(artifact_id)

    # Map output paths to their producer step numbers (for the latest runs)
    output_path_to_step: dict[str, int] = {}
    for _artifact_id, artifact_info in all_artifacts.items():
        output_path_to_step[artifact_info["path"]] = artifact_info["producer_step"]

    # Second pass: collect consumer relationships
    for num, step in latest_by_step.items():
        inputs = db_ctx.jobs.get_inputs(step["id"], db_ctx.artifacts)
        for inp in inputs:
            artifact_id = str(inp.get("artifact_id", ""))
            if artifact_id and artifact_id in all_artifacts:
                producer_step = all_artifacts[artifact_id]["producer_step"]
                if producer_step != num and producer_step < num:
                    if artifact_id not in artifact_consumers:
                        artifact_consumers[artifact_id] = []
                    if num not in artifact_consumers[artifact_id]:
                        artifact_consumers[artifact_id].append(num)

    for step in steps_to_show:
        job_id = step["id"]
        step_number = step["step_number"]

        # Get inputs and outputs
        inputs = db_ctx.jobs.get_inputs(job_id, db_ctx.artifacts)
        outputs = db_ctx.jobs.get_outputs(job_id, db_ctx.artifacts)

        # Calculate consumed count (inputs that came from other tracked jobs)
        consumed = 0
        dependencies = []
        for inp in inputs:
            path = inp.get("path") or inp.get("first_seen_path")
            if path and path in output_path_to_step:
                producer_step = output_path_to_step[path]
                if producer_step != step_number and producer_step < step_number:
                    consumed += 1
                    if producer_step not in dependencies:
                        dependencies.append(producer_step)

        # Determine state
        is_latest = step == latest_by_step.get(step_number)
        is_stale = step_number in stale_steps

        if expanded and not is_latest:
            state = "superseded"
        elif is_stale:
            state = "stale"
        elif is_latest:
            state = "active"  # On the current path
        else:
            state = "cached"

        node = {
            "step_number": step_number,
            "job_id": job_id,
            "job_uid": step.get("job_uid"),
            "command": step.get("command", ""),
            "step_name": step.get("step_name"),
            "state": state,
            "is_build": step.get("job_type") == "build",
            "exit_code": step.get("exit_code"),
            "metrics": {
                "inputs": len(inputs),
                "outputs": len(outputs),
                "consumed": consumed,
            },
            "dependencies": sorted(dependencies),
        }
        nodes.append(node)

    # Build step state map for artifact state computation
    step_states: dict[int, str] = {}
    for node in nodes:
        step_states[node["step_number"]] = node["state"]

    # Propagate superseded state downstream (in expanded view)
    # A step that depends on a superseded step should also be superseded
    if expanded:
        superseded_steps = {node["step_number"] for node in nodes if node["state"] == "superseded"}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                step_num = node["step_number"]
                if step_num not in superseded_steps:
                    # Check if any dependency is superseded
                    for dep in node["dependencies"]:
                        if dep in superseded_steps:
                            superseded_steps.add(step_num)
                            step_states[step_num] = "superseded"
                            node["state"] = "superseded"
                            changed = True
                            break

    # Identify leaf steps (no downstream consumers)
    step_numbers = {n["step_number"] for n in nodes}
    downstream_exists = set()
    for node in nodes:
        for dep in node["dependencies"]:
            downstream_exists.add(dep)
    leaf_steps = step_numbers - downstream_exists

    # Compute artifact states and build artifact list
    artifacts = []
    stale_artifact_count = 0
    superseded_artifact_count = 0

    for artifact_id, artifact_info in all_artifacts.items():
        producer_step = artifact_info["producer_step"]
        consumers = artifact_consumers.get(artifact_id, [])
        is_terminal = producer_step in leaf_steps

        # Determine artifact state
        producer_state = step_states.get(producer_step, "active")

        if producer_state == "stale":
            artifact_state = "stale"
            stale_artifact_count += 1
        elif producer_state == "superseded":
            artifact_state = "superseded"
            superseded_artifact_count += 1
        elif not consumers and not is_terminal:
            # Has no consumers and is not a terminal artifact -> orphaned
            artifact_state = "orphaned"
        else:
            # Producer is active and artifact has consumers or is terminal
            artifact_state = "active"

        # Check for superseded artifacts at same path
        superseded_by = None
        path = artifact_info["path"]
        if path in artifacts_by_path and len(artifacts_by_path[path]) > 1:
            # Multiple artifacts at same path - find if this one is superseded
            path_artifacts = artifacts_by_path[path]
            if artifact_id != path_artifacts[-1]:
                # This is not the latest artifact at this path
                superseded_by = path_artifacts[-1]
                if artifact_state != "superseded":
                    artifact_state = "superseded"
                    superseded_artifact_count += 1

        # Filter based on show_artifacts flag
        if not show_artifacts and not is_terminal:
            continue

        artifact_entry = {
            "path": path,
            "hash": artifact_info["hash"],
            "is_stale": producer_step in stale_steps,
            "producer_step": producer_step,
            "state": artifact_state,
            "artifact_id": artifact_id,
            "consumer_steps": sorted(consumers),
            "is_terminal": is_terminal,
            "superseded_by": superseded_by,
        }
        artifacts.append(artifact_entry)

    # Apply stale_only filter
    if stale_only:
        nodes = [n for n in nodes if n["state"] == "stale"]
        artifacts = [a for a in artifacts if a["state"] in ("stale", "superseded")]

    return {
        "nodes": nodes,
        "artifacts": artifacts,
        "stale_count": len(stale_steps),
        "total_steps": len(steps_by_number),
        "is_expanded": expanded,
        "session_id": session_id,
        "stale_artifact_count": stale_artifact_count,
        "superseded_artifact_count": superseded_artifact_count,
    }


@click.command("dag")
@click.option(
    "--expanded",
    is_flag=True,
    default=False,
    help="Show full execution history with all reruns",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Plain text output (no ANSI colors)",
)
@click.option(
    "--show-artifacts",
    is_flag=True,
    default=False,
    help="Show intermediate artifacts between steps (default: terminal only)",
)
@click.option(
    "--stale-only",
    is_flag=True,
    default=False,
    help="Filter to show only stale steps and artifacts",
)
@click.pass_obj
@require_init
def dag(
    ctx: RoarContext,
    expanded: bool,
    output_json: bool,
    no_color: bool,
    show_artifacts: bool,
    stale_only: bool,
) -> None:
    """Display the pipeline DAG for the current session.

    Shows all steps in the current session as a directed acyclic graph (DAG),
    with their dependencies, states, and I/O metrics.

    \b
    Each node shows:
    - Step reference (@N for run steps, @BN for build steps)
    - Command or step name
    - Metrics: in (inputs read), out (outputs written), cons (consumed from prior steps)
    - State marker: * for stale steps

    \b
    Artifact states:
    - active: Produced by active step, on the execution path
    - stale: Produced by stale step
    - superseded: Old version replaced by re-run
    - orphaned: Not consumed by any active step

    \b
    Examples:

        roar dag                  # Compact view with colors

        roar dag --expanded       # Show all executions including reruns

        roar dag --json           # Machine-readable JSON output

        roar dag --no-color       # Plain text for piping

        roar dag --show-artifacts # Show intermediate artifacts

        roar dag --stale-only     # Filter to only stale steps/artifacts
    """
    # Import here to avoid circular imports
    from ...core.models.dag import (
        DagArtifactInfo,
        DagArtifactState,
        DagNodeInfo,
        DagNodeMetrics,
        DagNodeState,
        DagVisualization,
    )
    from ...presenters.dag_renderer import DagRenderer

    with create_database_context(ctx.roar_dir) as db_ctx:
        # Get active session
        session = db_ctx.sessions.get_active()
        if not session:
            click.echo("No active session. Run 'roar init' or 'roar run' first.")
            raise SystemExit(1)

        session_id = session["id"]

        # Get DAG data
        dag_data = _get_dag_data(
            db_ctx,
            session_id,
            expanded=expanded,
            show_artifacts=show_artifacts,
            stale_only=stale_only,
        )

        # Convert to Pydantic models
        nodes = [
            DagNodeInfo(
                step_number=n["step_number"],
                job_id=n["job_id"],
                job_uid=n["job_uid"],
                command=n["command"],
                step_name=n["step_name"],
                state=DagNodeState(n["state"]),
                is_build=n["is_build"],
                exit_code=n["exit_code"],
                metrics=DagNodeMetrics(**n["metrics"]),
                dependencies=n["dependencies"],
            )
            for n in dag_data["nodes"]
        ]

        artifacts = [
            DagArtifactInfo(
                path=a["path"],
                hash=a["hash"],
                is_stale=a["is_stale"],
                producer_step=a["producer_step"],
                state=DagArtifactState(a["state"]),
                artifact_id=a["artifact_id"],
                consumer_steps=a["consumer_steps"],
                is_terminal=a["is_terminal"],
                superseded_by=a["superseded_by"],
            )
            for a in dag_data["artifacts"]
        ]

        dag_viz = DagVisualization(
            nodes=nodes,
            artifacts=artifacts,
            stale_count=dag_data["stale_count"],
            total_steps=dag_data["total_steps"],
            is_expanded=dag_data["is_expanded"],
            session_id=dag_data["session_id"],
            stale_artifact_count=dag_data["stale_artifact_count"],
            superseded_artifact_count=dag_data["superseded_artifact_count"],
        )

        # Render output
        use_color = not no_color and sys.stdout.isatty()
        renderer = DagRenderer(use_color=use_color)

        if output_json:
            json_output = renderer.render_json(dag_viz)
            click.echo(json.dumps(json_output, indent=2))
        else:
            text_output = renderer.render(dag_viz)
            click.echo(text_output)
