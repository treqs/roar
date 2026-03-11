"""DAG data builder for session visualization."""

from __future__ import annotations

from typing import Any, cast

from ..db.context import optional_repo

_STEP_NOISE_COMMANDS = {
    "ray_task:unknown",
    "ray_task:__init__",
    "ray_task:s3_proxy",
    "ray_task:s3_driver_proxy",
    "ray_task:RoarNodeAgent.__init__",
}


class DagDataBuilder:
    """Build DAG visualization data from session."""

    def __init__(self, db_ctx: Any, session_id: int):
        self._db_ctx = db_ctx
        self._session_id = session_id

    def build(
        self,
        expanded: bool = False,
        show_artifacts: bool = False,
        stale_only: bool = False,
    ) -> dict[str, Any]:
        """Build DAG visualization data.

        Args:
            expanded: Whether to include superseded job executions.
            show_artifacts: Whether to include intermediate artifacts.
        stale_only: Whether to filter to only stale steps and artifacts.

        Returns:
            Dictionary with DAG visualization data.
        """
        session_labels = self._current_labels("dag", session_id=self._session_id)
        steps = self._fetch_steps()
        if not steps:
            return self._empty_result(expanded, session_labels)

        stale_steps = self._fetch_stale_steps()
        steps_by_number = self._group_by_step_number(steps)
        steps_to_show = self._select_steps(steps_by_number, steps, expanded)

        all_artifacts, artifacts_by_path = self._collect_artifacts(steps_by_number)
        output_path_to_step = self._map_output_paths(all_artifacts)
        artifact_consumers = self._collect_consumers(steps_by_number, all_artifacts)

        nodes = self._build_nodes(
            steps_to_show, steps_by_number, stale_steps, output_path_to_step, expanded
        )

        step_states = self._propagate_superseded(nodes, expanded)

        # Identify leaf steps (no downstream consumers)
        step_numbers = {n["step_number"] for n in nodes}
        downstream_exists: set[int] = set()
        for node in nodes:
            for dep in node["dependencies"]:
                downstream_exists.add(dep)
        leaf_steps = step_numbers - downstream_exists

        artifacts, stale_artifact_count, superseded_artifact_count = self._build_artifacts(
            all_artifacts,
            artifact_consumers,
            step_states,
            stale_steps,
            artifacts_by_path,
            leaf_steps,
            show_artifacts,
        )

        # Apply stale_only filter
        if stale_only:
            nodes = [n for n in nodes if n["state"] == "stale"]
            artifacts = [a for a in artifacts if a["state"] in ("stale", "superseded")]

        return {
            "nodes": nodes,
            "artifacts": artifacts,
            "labels": session_labels,
            "stale_count": len(stale_steps),
            "total_steps": len(steps_by_number),
            "is_expanded": expanded,
            "session_id": self._session_id,
            "stale_artifact_count": stale_artifact_count,
            "superseded_artifact_count": superseded_artifact_count,
        }

    def _empty_result(self, expanded: bool, session_labels: dict[str, Any]) -> dict[str, Any]:
        """Return empty DAG data when there are no steps."""
        return {
            "nodes": [],
            "artifacts": [],
            "labels": session_labels,
            "stale_count": 0,
            "total_steps": 0,
            "is_expanded": expanded,
            "session_id": self._session_id,
            "stale_artifact_count": 0,
            "superseded_artifact_count": 0,
        }

    def _fetch_steps(self) -> list[dict]:
        """Get steps from the database context."""
        return self._db_ctx.sessions.get_steps(self._session_id)

    def _fetch_stale_steps(self) -> set[int]:
        """Get stale step numbers."""
        return set(self._db_ctx.session_service.get_stale_steps(self._session_id))

    def _group_by_step_number(self, steps: list[dict]) -> dict[int, list[dict]]:
        """Group steps by step_number and sort each group by timestamp."""
        steps_by_number: dict[int, list[dict]] = {}
        for step in steps:
            num = step["step_number"]
            if num not in steps_by_number:
                steps_by_number[num] = []
            steps_by_number[num].append(step)

        for num in steps_by_number:
            steps_by_number[num].sort(key=lambda s: s["timestamp"])

        return steps_by_number

    def _step_sort_key(self, step: dict) -> tuple[int, float, int]:
        job_type = step.get("job_type")
        command = str(step.get("command") or "")
        script = str(step.get("script") or "")
        parent_job_uid = str(step.get("parent_job_uid") or "")
        is_phase_wrapper = (
            command.startswith("ray_task:")
            and command not in _STEP_NOISE_COMMANDS
            and bool(parent_job_uid)
            and bool(script)
            and "." not in script
        )
        if job_type in (None, "run"):
            priority = 6
        elif is_phase_wrapper:
            priority = 5
        elif command and command not in _STEP_NOISE_COMMANDS:
            priority = 4
        elif command in _STEP_NOISE_COMMANDS:
            priority = 1
        else:
            priority = 2
        return (
            priority,
            float(step.get("timestamp") or 0.0),
            int(step.get("id") or 0),
        )

    def _representative_steps(self, steps_by_number: dict[int, list[dict]]) -> dict[int, dict]:
        return {
            num: max(group, key=self._step_sort_key)
            for num, group in steps_by_number.items()
            if group
        }

    def _select_steps(
        self,
        steps_by_number: dict[int, list[dict]],
        steps: list[dict],
        expanded: bool,
    ) -> list[dict]:
        """Pick which steps to show based on expanded mode."""
        if expanded:
            return steps
        representative_by_step = self._representative_steps(steps_by_number)
        return [representative_by_step[num] for num in sorted(representative_by_step.keys())]

    def _collect_artifacts(
        self, steps_by_number: dict[int, list[dict]]
    ) -> tuple[dict[str, dict], dict[str, list[str]]]:
        """Build all_artifacts and artifacts_by_path from latest step outputs."""
        latest_by_step = self._representative_steps(steps_by_number)

        all_artifacts: dict[str, dict] = {}
        artifacts_by_path: dict[str, list[str]] = {}

        for num, step in latest_by_step.items():
            outputs = self._db_ctx.jobs.get_outputs(step["id"])
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

        return all_artifacts, artifacts_by_path

    def _map_output_paths(self, all_artifacts: dict[str, dict]) -> dict[str, int]:
        """Map output paths to their producer step numbers."""
        output_path_to_step: dict[str, int] = {}
        for _artifact_id, artifact_info in all_artifacts.items():
            output_path_to_step[artifact_info["path"]] = artifact_info["producer_step"]
        return output_path_to_step

    def _collect_consumers(
        self,
        steps_by_number: dict[int, list[dict]],
        all_artifacts: dict[str, dict],
    ) -> dict[str, list[int]]:
        """Build consumer relationships from step inputs."""
        latest_by_step = self._representative_steps(steps_by_number)

        artifact_consumers: dict[str, list[int]] = {}

        for num, step in latest_by_step.items():
            inputs = self._db_ctx.jobs.get_inputs(step["id"])
            for inp in inputs:
                artifact_id = str(inp.get("artifact_id", ""))
                if artifact_id and artifact_id in all_artifacts:
                    producer_step = all_artifacts[artifact_id]["producer_step"]
                    if producer_step != num and producer_step < num:
                        if artifact_id not in artifact_consumers:
                            artifact_consumers[artifact_id] = []
                        if num not in artifact_consumers[artifact_id]:
                            artifact_consumers[artifact_id].append(num)

        return artifact_consumers

    def _build_nodes(
        self,
        steps_to_show: list[dict],
        steps_by_number: dict[int, list[dict]],
        stale_steps: set[int],
        output_path_to_step: dict[str, int],
        expanded: bool,
    ) -> list[dict]:
        """Build node data for each step to show."""
        latest_by_step = self._representative_steps(steps_by_number)
        nodes = []

        for step in steps_to_show:
            job_id = step["id"]
            step_number = step["step_number"]

            # Get inputs and outputs
            inputs = self._db_ctx.jobs.get_inputs(job_id)
            outputs = self._db_ctx.jobs.get_outputs(job_id)
            primitive_inputs = [inp for inp in inputs if inp.get("kind") != "composite"]
            primitive_outputs = [out for out in outputs if out.get("kind") != "composite"]

            # Calculate consumed count (inputs that came from other tracked jobs)
            consumed = 0
            dependencies: list[int] = []
            for inp in primitive_inputs:
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
                state = "active"
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
                    "inputs": len(primitive_inputs),
                    "outputs": len(primitive_outputs),
                    "consumed": consumed,
                },
                "dependencies": sorted(dependencies),
                "labels": self._current_labels("job", job_id=int(job_id)),
            }
            nodes.append(node)

        return nodes

    def _propagate_superseded(self, nodes: list[dict], expanded: bool) -> dict[int, str]:
        """Propagate superseded states downstream and return step_states map."""
        step_states: dict[int, str] = {}
        for node in nodes:
            step_states[node["step_number"]] = node["state"]

        if expanded:
            superseded_steps = {
                node["step_number"] for node in nodes if node["state"] == "superseded"
            }
            changed = True
            while changed:
                changed = False
                for node in nodes:
                    step_num = node["step_number"]
                    if step_num not in superseded_steps:
                        for dep in node["dependencies"]:
                            if dep in superseded_steps:
                                superseded_steps.add(step_num)
                                step_states[step_num] = "superseded"
                                node["state"] = "superseded"
                                changed = True
                                break

        return step_states

    def _build_artifacts(
        self,
        all_artifacts: dict[str, dict],
        artifact_consumers: dict[str, list[int]],
        step_states: dict[int, str],
        stale_steps: set[int],
        artifacts_by_path: dict[str, list[str]],
        leaf_steps: set[int],
        show_artifacts: bool,
    ) -> tuple[list[dict], int, int]:
        """Build artifact entries with state computation.

        Returns:
            Tuple of (artifact list, stale_artifact_count, superseded_artifact_count).
        """
        artifacts: list[dict] = []
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
                artifact_state = "orphaned"
            else:
                artifact_state = "active"

            # Check for superseded artifacts at same path
            superseded_by = None
            path = artifact_info["path"]
            if path in artifacts_by_path and len(artifacts_by_path[path]) > 1:
                path_artifacts = artifacts_by_path[path]
                if artifact_id != path_artifacts[-1]:
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
                "labels": self._current_labels("artifact", artifact_id=artifact_id),
            }
            artifacts.append(artifact_entry)

        return artifacts, stale_artifact_count, superseded_artifact_count

    def _current_labels(
        self,
        entity_type: str,
        *,
        session_id: int | None = None,
        job_id: int | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        labels_repo = optional_repo(self._db_ctx, "labels")
        if labels_repo is None:
            return {}

        current = cast(Any, labels_repo).get_current(
            entity_type,
            session_id=session_id,
            job_id=job_id,
            artifact_id=artifact_id,
        )
        if not isinstance(current, dict):
            return {}
        metadata = current.get("metadata")
        return metadata if isinstance(metadata, dict) else {}
