"""
ASCII DAG renderer for terminal output.

Renders DAG visualizations using box-drawing characters with
optional color support for terminal display.
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING, ClassVar

from ..core.models.dag import DagNodeState, DagVisualization
from .formatting import truncate_command

if TYPE_CHECKING:
    from ..core.models.dag import DagArtifactInfo, DagNodeInfo


class DagRenderer:
    """
    ASCII renderer for DAG visualizations.

    Renders a tree-style layout using box-drawing characters,
    with optional color support for terminal display.
    """

    # Box-drawing characters
    BRANCH = "\u251c\u2500\u2500>"  # ├──>
    CORNER = "\u2514\u2500\u2500>"  # └──>
    PIPE = "\u2502"  # │
    SPACE = " "

    # ANSI color codes
    COLORS: ClassVar[dict[str, str]] = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "green": "\033[92m",
        "blue": "\033[94m",
        "red": "\033[91m",
        "yellow": "\033[93m",
        "gray": "\033[90m",
        "magenta": "\033[95m",
    }

    def __init__(self, use_color: bool = True, terminal_width: int | None = None):
        """
        Initialize DAG renderer.

        Args:
            use_color: Whether to use ANSI color codes
            terminal_width: Terminal width override (auto-detected if None)
        """
        self._use_color = use_color and sys.stdout.isatty()
        if terminal_width is None:
            try:
                self._terminal_width = shutil.get_terminal_size().columns
            except Exception:
                self._terminal_width = 80
        else:
            self._terminal_width = terminal_width

    def render(self, dag: DagVisualization) -> str:
        """
        Render DAG visualization as ASCII string.

        Args:
            dag: DAG visualization data

        Returns:
            ASCII string representation of the DAG
        """
        lines: list[str] = []

        # Header
        stale_parts = []
        if dag.stale_count > 0:
            stale_parts.append(f"{dag.stale_count} stale steps")
        if dag.stale_artifact_count > 0:
            stale_parts.append(f"{dag.stale_artifact_count} stale artifacts")
        if dag.superseded_artifact_count > 0:
            stale_parts.append(f"{dag.superseded_artifact_count} superseded")
        stale_info = f" ({', '.join(stale_parts)})" if stale_parts else ""

        # Calculate total job count (nodes shown)
        total_jobs = len(dag.nodes)
        if dag.is_expanded and total_jobs != dag.total_steps:
            header = f"Pipeline: {dag.total_steps} steps ({total_jobs} runs shown)"
        else:
            header = f"Pipeline: {dag.total_steps} steps"
        lines.append(f"{header}{stale_info}")
        lines.append("")

        if not dag.nodes:
            lines.append("No steps in pipeline.")
            return "\n".join(lines)

        # Use job_id as unique identifier for nodes (handles expanded view)
        node_by_id: dict[int, DagNodeInfo] = {node.job_id: node for node in dag.nodes}

        # Build mapping from step_number to job_ids (for dependency resolution)
        step_to_job_ids: dict[int, list[int]] = {}
        for node in dag.nodes:
            if node.step_number not in step_to_job_ids:
                step_to_job_ids[node.step_number] = []
            step_to_job_ids[node.step_number].append(node.job_id)

        # Build children mapping by job_id
        children_by_id: dict[int, list[int]] = {node.job_id: [] for node in dag.nodes}

        # For each node, find which nodes depend on it
        for node in dag.nodes:
            for dep_step in node.dependencies:
                # Find job_ids for this dependency step
                dep_job_ids = step_to_job_ids.get(dep_step, [])
                for dep_job_id in dep_job_ids:
                    if dep_job_id in children_by_id:
                        children_by_id[dep_job_id].append(node.job_id)

        # Find root nodes (no dependencies or deps not in current dag)
        root_job_ids = []
        for node in dag.nodes:
            if not node.dependencies:
                root_job_ids.append(node.job_id)
            else:
                # Check if all dependencies are outside this DAG
                has_dep_in_dag = False
                for dep_step in node.dependencies:
                    if dep_step in step_to_job_ids:
                        has_dep_in_dag = True
                        break
                if not has_dep_in_dag:
                    root_job_ids.append(node.job_id)

        # Sort roots by step number, then job_id
        root_job_ids.sort(key=lambda jid: (node_by_id[jid].step_number, jid))

        # Track which nodes we've rendered (by job_id)
        rendered: set[int] = set()

        # Render tree from each root
        for i, root_job_id in enumerate(root_job_ids):
            is_last_root = i == len(root_job_ids) - 1
            self._render_node_by_id(
                lines,
                node_by_id,
                children_by_id,
                root_job_id,
                prefix="",
                is_last=is_last_root,
                rendered=rendered,
                dag=dag,
            )

        # Add legend
        lines.append("")
        legend_parts = []
        if dag.stale_count > 0:
            legend_parts.append("* = stale")
        if any(n.is_build for n in dag.nodes):
            legend_parts.append("B = build step")
        if dag.is_expanded and any(
            (n.state.value if hasattr(n.state, "value") else n.state) == "superseded"
            for n in dag.nodes
        ):
            legend_parts.append("[superseded] = previous run")
        if legend_parts:
            lines.append(f"Legend: {', '.join(legend_parts)}")

        return "\n".join(lines)

    def _render_node_by_id(
        self,
        lines: list[str],
        node_by_id: dict[int, DagNodeInfo],
        children_by_id: dict[int, list[int]],
        job_id: int,
        prefix: str,
        is_last: bool,
        rendered: set[int],
        dag: DagVisualization,
    ) -> None:
        """Recursively render a node and its children."""
        if job_id in rendered:
            return
        rendered.add(job_id)

        node = node_by_id.get(job_id)
        if not node:
            return

        # Format the node line
        node_line = self._format_node(node, prefix, is_last)
        lines.append(node_line)

        # Get children of this node
        children = sorted(children_by_id.get(job_id, []))

        # Calculate new prefix for children
        if prefix:
            new_prefix = prefix[:-4] + ("    " if is_last else f"{self.PIPE}   ")
        else:
            new_prefix = ""

        # Render children
        for i, child_job_id in enumerate(children):
            if child_job_id in rendered:
                continue
            child_is_last = i == len(children) - 1

            # Check if all dependencies of this child are rendered
            child_node = node_by_id.get(child_job_id)
            if child_node:
                # For dependency checking, we need to see if all dependency steps have
                # at least one job rendered
                deps_satisfied = True
                for dep_step in child_node.dependencies:
                    # Find any job with this step number that's been rendered
                    dep_rendered = any(
                        jid in rendered
                        for jid, n in node_by_id.items()
                        if n.step_number == dep_step
                    )
                    if not dep_rendered:
                        deps_satisfied = False
                        break

                if deps_satisfied:
                    child_prefix = new_prefix + (self.CORNER if child_is_last else self.BRANCH)
                    self._render_node_by_id(
                        lines,
                        node_by_id,
                        children_by_id,
                        child_job_id,
                        child_prefix,
                        child_is_last,
                        rendered,
                        dag,
                    )

        # Render artifact output for terminal nodes
        if not children or all(c in rendered for c in children):
            for artifact in dag.artifacts:
                if artifact.producer_step == node.step_number:
                    artifact_prefix = new_prefix + ("    " if is_last else f"{self.PIPE}   ")
                    artifact_line = self._format_artifact(artifact, artifact_prefix)
                    lines.append(artifact_line)

    def _format_node(self, node: DagNodeInfo, prefix: str, is_last: bool) -> str:
        """Format a single node line."""
        # Step reference (@N or @BN)
        step_ref = f"@B{node.step_number}" if node.is_build else f"@{node.step_number}"

        # Determine state marker FIRST to account for its width
        state_value = node.state.value if hasattr(node.state, "value") else node.state
        if state_value == "superseded":
            stale_marker = " [superseded]"
        elif state_value == "stale":
            stale_marker = " *"
        else:
            stale_marker = ""

        # Command (truncated if needed)
        # Calculate available space: total - prefix - step_ref - metrics - marker - padding
        metrics_str = (
            f"in:{node.metrics.inputs} out:{node.metrics.outputs} cons:{node.metrics.consumed}"
        )
        base_length = (
            len(prefix) + len(step_ref) + len(metrics_str) + len(stale_marker) + 6
        )  # padding/spacing
        available_width = max(20, self._terminal_width - base_length)

        # Use step_name if available, otherwise truncate command
        display_name = node.step_name or node.command
        truncated_cmd = truncate_command(display_name, available_width)

        # Build the line
        line = f"{prefix}{step_ref} {truncated_cmd:<{available_width}}  {metrics_str}{stale_marker}"

        # Apply colors
        line = self._apply_color(line, node.state, step_ref, stale_marker)

        return line

    def _format_artifact(self, artifact: DagArtifactInfo, prefix: str) -> str:
        """Format an artifact line."""
        # Extract just the filename
        path = artifact.path
        if "/" in path:
            path = path.split("/")[-1]

        # Get state value (handle both enum and string)
        state_value = artifact.state.value if hasattr(artifact.state, "value") else artifact.state

        # Build state marker
        state_marker = ""
        if state_value == "stale":
            state_marker = " [stale]"
        elif state_value == "superseded":
            state_marker = " [superseded]"
        elif state_value == "orphaned":
            state_marker = " [orphaned]"

        # Add terminal indicator if not terminal
        terminal_marker = "" if artifact.is_terminal else " (intermediate)"

        line = f"{prefix}{self.CORNER} ({path}){state_marker}{terminal_marker}"

        if self._use_color:
            line = self._apply_artifact_color(line, state_value)

        return line

    def _apply_artifact_color(self, line: str, state: str) -> str:
        """Apply ANSI color codes based on artifact state."""
        if not self._use_color:
            return line

        if state == "active":
            return f"{self.COLORS['green']}{line}{self.COLORS['reset']}"
        elif state == "stale":
            return f"{self.COLORS['red']}{line}{self.COLORS['reset']}"
        elif state == "superseded":
            return f"{self.COLORS['dim']}{self.COLORS['gray']}{line}{self.COLORS['reset']}"
        elif state == "orphaned":
            return f"{self.COLORS['yellow']}{line}{self.COLORS['reset']}"
        else:
            return f"{self.COLORS['magenta']}{line}{self.COLORS['reset']}"

    def _apply_color(
        self, line: str, state: DagNodeState | str, step_ref: str, stale_marker: str
    ) -> str:
        """Apply ANSI color codes based on node state."""
        if not self._use_color:
            return line

        # Normalize state to string for comparison
        state_value = state.value if hasattr(state, "value") else state

        if state_value == "active":
            return f"{self.COLORS['green']}{self.COLORS['bold']}{line}{self.COLORS['reset']}"
        elif state_value == "cached":
            return f"{self.COLORS['blue']}{line}{self.COLORS['reset']}"
        elif state_value == "stale":
            return f"{self.COLORS['red']}{line}{self.COLORS['reset']}"
        elif state_value == "superseded":
            return f"{self.COLORS['dim']}{self.COLORS['gray']}{line}{self.COLORS['reset']}"

        return line

    def render_json(self, dag: DagVisualization) -> dict:
        """
        Render DAG visualization as JSON-serializable dict.

        Args:
            dag: DAG visualization data

        Returns:
            JSON-serializable dictionary
        """
        return {
            "session_id": dag.session_id,
            "total_steps": dag.total_steps,
            "stale_count": dag.stale_count,
            "stale_artifact_count": dag.stale_artifact_count,
            "superseded_artifact_count": dag.superseded_artifact_count,
            "is_expanded": dag.is_expanded,
            "nodes": [
                {
                    "step_number": node.step_number,
                    "job_id": node.job_id,
                    "job_uid": node.job_uid,
                    "command": node.command,
                    "step_name": node.step_name,
                    # state may be enum or string depending on Pydantic serialization
                    "state": node.state.value if hasattr(node.state, "value") else node.state,
                    "is_build": node.is_build,
                    "exit_code": node.exit_code,
                    "metrics": {
                        "inputs": node.metrics.inputs,
                        "outputs": node.metrics.outputs,
                        "consumed": node.metrics.consumed,
                    },
                    "dependencies": node.dependencies,
                }
                for node in dag.nodes
            ],
            "artifacts": [
                {
                    "path": artifact.path,
                    "hash": artifact.hash,
                    "is_stale": artifact.is_stale,
                    "producer_step": artifact.producer_step,
                    "state": (
                        artifact.state.value if hasattr(artifact.state, "value") else artifact.state
                    ),
                    "artifact_id": artifact.artifact_id,
                    "consumer_steps": artifact.consumer_steps,
                    "is_terminal": artifact.is_terminal,
                    "superseded_by": artifact.superseded_by,
                }
                for artifact in dag.artifacts
            ],
        }
