"""
Show command renderer for displaying session, job, and artifact details.

Handles all output formatting for the show command.
Follows SRP: only handles presentation, not data fetching.
"""

from __future__ import annotations

import json

from ..application.label_rendering import render_label_lines
from .formatting import format_duration, format_size, format_timestamp


class ShowRenderer:
    """
    Renders show command output as plain text strings.

    All methods accept pre-fetched data and return formatted strings,
    keeping presentation separate from data access.
    """

    @staticmethod
    def _render_labels(lines: list[str], metadata: dict | None) -> None:
        if not metadata:
            return
        lines.append("\nLabels:")
        lines.extend(render_label_lines(metadata, indent="  "))

    def render_session(self, session: dict, jobs: list[dict], labels: dict | None = None) -> str:
        """Render session overview with job listing.

        Args:
            session: Session dict with 'hash', 'created_at', 'git_repo', 'git_commit_start'.
            jobs: List of job dicts for this session.

        Returns:
            Formatted string for display.
        """
        lines: list[str] = []

        lines.append(f"\nSession: {session['hash']}")
        lines.append(f"Created: {format_timestamp(session['created_at'])}")
        if session.get("git_repo"):
            lines.append(f"Git: {session['git_repo']}")
        if session.get("git_commit_start"):
            lines.append(f"Commit: {session['git_commit_start']}")

        self._render_labels(lines, labels)

        if not jobs:
            lines.append("\nNo jobs in this session.")
            return "\n".join(lines)

        lines.append(f"\nJobs ({len(jobs)}):\n")

        # Header
        lines.append(f"{'STEP':<6}  {'JOB UID':<8}  {'STATUS':<6}  {'COMMAND'}")
        lines.append("-" * 60)

        # Jobs ordered by step number (oldest first)
        for job in reversed(jobs):
            step = f"@{job['step_number']}" if job["step_number"] else "-"
            if job.get("job_type") == "build":
                step = f"@B{job['step_number']}" if job["step_number"] else "-"

            uid = job["job_uid"] or "?"

            if job["exit_code"] is None:
                status = "?"
            elif job["exit_code"] == 0:
                status = "OK"
            else:
                status = "FAIL"

            command = job["command"] or ""
            # Truncate long commands for table display
            if len(command) > 50:
                command = command[:47] + "..."

            lines.append(f"{step:<6}  {uid:<8}  {status:<6}  {command}")

        return "\n".join(lines)

    def render_job(
        self,
        job: dict,
        inputs: list[dict],
        outputs: list[dict],
        labels: dict | None = None,
        composite_details: dict | None = None,
    ) -> str:
        """Render detailed job view with artifacts.

        Args:
            job: Job dict. Must include 'metadata' and 'telemetry' as
                 already-parsed dicts (or None), not raw JSON strings.
            inputs: List of input artifact dicts.
            outputs: List of output artifact dicts.
            composite_details: Optional dict with extra composite info (unused currently,
                               reserved for future extension).

        Returns:
            Formatted string for display.
        """
        lines: list[str] = []

        lines.append(f"\nJob: {job['job_uid']}")

        step_ref = ""
        if job["step_number"]:
            prefix = "@B" if job.get("job_type") == "build" else "@"
            step_ref = f" ({prefix}{job['step_number']})"
        lines.append(f"Step: {job['step_number'] or '-'}{step_ref}")

        if job.get("step_name"):
            lines.append(f"Name: {job['step_name']}")
        if job.get("step_identity"):
            lines.append(f"Identity: {job['step_identity']}")

        lines.append(f"Timestamp: {format_timestamp(job['timestamp'])}")
        lines.append(f"Duration: {format_duration(job['duration_seconds'])}")

        if job["exit_code"] is None:
            status = "Unknown"
        elif job["exit_code"] == 0:
            status = "Success"
        else:
            status = f"Failed (exit {job['exit_code']})"
        lines.append(f"Status: {status}")

        self._render_labels(lines, labels)

        lines.append(f"\nCommand: {job['command']}")

        # Git info
        if job.get("git_commit"):
            lines.append(f"\nGit commit: {job['git_commit']}")
        if job.get("git_branch"):
            lines.append(f"Git branch: {job['git_branch']}")

        # Metadata (what gets registered with GLaaS)
        meta = job.get("metadata")
        if meta and isinstance(meta, dict):
            lines.append("\nMetadata:")

            # Working directory
            if meta.get("cwd"):
                lines.append(f"  Working dir: {meta['cwd']}")

            # Runtime info
            runtime = meta.get("runtime", {})
            if runtime.get("hostname"):
                lines.append(f"  Hostname: {runtime['hostname']}")
            if runtime.get("os"):
                os_info = runtime["os"]
                lines.append(f"  OS: {os_info.get('system', '')} {os_info.get('release', '')}")
            if runtime.get("python"):
                lines.append(f"  Python: {runtime['python'].get('version', '')}")

            # Hardware
            if runtime.get("gpu"):
                gpus = runtime["gpu"]
                for i, gpu in enumerate(gpus):
                    gpu_str = (
                        f"  GPU {i}: {gpu.get('name', 'unknown')} ({gpu.get('memory_mb', '?')} MB)"
                    )
                    if gpu.get("compute_cap"):
                        gpu_str += f", compute cap {gpu['compute_cap']}"
                    lines.append(gpu_str)
            if runtime.get("cuda"):
                cuda = runtime["cuda"]
                cuda_parts = []
                if cuda.get("cuda_version"):
                    cuda_parts.append(f"CUDA {cuda['cuda_version']}")
                if cuda.get("driver_version"):
                    cuda_parts.append(f"driver {cuda['driver_version']}")
                if cuda.get("cudnn_version"):
                    cuda_parts.append(f"cuDNN {cuda['cudnn_version']}")
                if cuda_parts:
                    lines.append(f"  CUDA: {', '.join(cuda_parts)}")
            if runtime.get("cpu"):
                cpu = runtime["cpu"]
                lines.append(
                    f"  CPU: {cpu.get('model', 'unknown')} ({cpu.get('count', '?')} cores)"
                )
            if runtime.get("memory"):
                mem = runtime["memory"]
                lines.append(f"  Memory: {mem.get('total_mb', '?')} MB")

            # Environment variables
            env_vars = runtime.get("env_vars", {})
            if env_vars:
                lines.append(f"\n  Environment Variables ({len(env_vars)}):")
                for name, value in sorted(env_vars.items()):
                    # Truncate long values
                    display_val = value if len(value) <= 60 else value[:57] + "..."
                    lines.append(f"    {name}={display_val}")

            # Packages
            packages = meta.get("packages", {})
            if packages and isinstance(packages, dict):
                for manager, pkgs in packages.items():
                    if pkgs and isinstance(pkgs, dict):
                        lines.append(f"\n  Packages ({manager}, {len(pkgs)}):")
                        for name, version in sorted(pkgs.items())[:15]:
                            if version:
                                lines.append(f"    {name}=={version}")
                            else:
                                lines.append(f"    {name}")
                        if len(pkgs) > 15:
                            lines.append(f"    ... and {len(pkgs) - 15} more")

        # Telemetry (external service links)
        telem = job.get("telemetry")
        if telem and isinstance(telem, dict):
            lines.append("\nTelemetry:")
            for name, url in telem.items():
                if isinstance(url, list):
                    for u in url:
                        lines.append(f"  {name}: {u}")
                else:
                    lines.append(f"  {name}: {url}")

        # Inputs
        if inputs:
            lines.append(f"\nInputs ({len(inputs)}):")
            for inp in inputs:
                lines.append(f"  {inp['path']}")
                lines.append(f"    Artifact: {inp['artifact_id']}")
                kind = inp.get("kind")
                component_count = inp.get("component_count")
                if isinstance(kind, str):
                    if kind == "composite" and isinstance(component_count, int):
                        lines.append(f"    Kind: {kind} ({component_count} components)")
                    else:
                        lines.append(f"    Kind: {kind}")
                lines.append(f"    Size: {format_size(inp['size'])}")
                for h in inp.get("hashes", []):
                    lines.append(f"    {h['algorithm']}: {h['digest']}")

        # Outputs
        if outputs:
            lines.append(f"\nOutputs ({len(outputs)}):")
            for out in outputs:
                lines.append(f"  {out['path']}")
                lines.append(f"    Artifact: {out['artifact_id']}")
                kind = out.get("kind")
                component_count = out.get("component_count")
                if isinstance(kind, str):
                    if kind == "composite" and isinstance(component_count, int):
                        lines.append(f"    Kind: {kind} ({component_count} components)")
                    else:
                        lines.append(f"    Kind: {kind}")
                lines.append(f"    Size: {format_size(out['size'])}")
                for h in out.get("hashes", []):
                    lines.append(f"    {h['algorithm']}: {h['digest']}")

        return "\n".join(lines)

    def render_artifact(
        self,
        artifact: dict,
        locations: list,
        jobs: dict,
        labels: dict | None = None,
        composite_summary: dict | None = None,
        components: list | None = None,
    ) -> str:
        """Render detailed artifact view.

        Args:
            artifact: Artifact dict with 'id', 'kind', 'component_count', 'size',
                      'first_seen_at', 'first_seen_path', 'hashes'.
            locations: List of location dicts with 'path'.
            jobs: Dict with 'produced_by' and 'consumed_by' lists of job dicts.
            composite_summary: Optional composite summary dict from composites repo.
            components: Optional list of component dicts from composites repo.

        Returns:
            Formatted string for display.
        """
        lines: list[str] = []

        lines.append(f"\nArtifact: {artifact['id']}")
        kind = artifact.get("kind")
        if isinstance(kind, str):
            lines.append(f"Kind: {kind}")
        component_count = artifact.get("component_count")
        if isinstance(component_count, int):
            lines.append(f"Components: {component_count}")
        lines.append(f"Size: {format_size(artifact['size'])}")
        lines.append(f"First seen: {format_timestamp(artifact['first_seen_at'])}")

        if artifact.get("first_seen_path"):
            lines.append(f"Original path: {artifact['first_seen_path']}")

        self._render_labels(lines, labels)

        # Hashes
        hashes = artifact.get("hashes", [])
        if hashes:
            lines.append("\nHashes:")
            for h in hashes:
                lines.append(f"  {h['algorithm']}: {h['digest']}")

        # Locations
        if locations:
            lines.append(f"\nLocations ({len(locations)}):")
            for loc in locations:
                lines.append(f"  {loc['path']}")

        # Jobs
        produced_by = jobs.get("produced_by", [])
        if produced_by:
            lines.append(f"\nProduced by ({len(produced_by)} job(s)):")
            for job in produced_by[:5]:
                cmd = (job.get("command") or "?")[:47]
                lines.append(f"  [{job.get('job_uid', '?')}] {cmd}")

        consumed_by = jobs.get("consumed_by", [])
        if consumed_by:
            lines.append(f"\nConsumed by ({len(consumed_by)} job(s)):")
            for job in consumed_by[:5]:
                cmd = (job.get("command") or "?")[:47]
                lines.append(f"  [{job.get('job_uid', '?')}] {cmd}")

        if composite_summary is not None and isinstance(composite_summary, dict):
            membership = composite_summary.get("membership_index")
            lines.append("\nComposite details:")
            if isinstance(composite_summary.get("component_count"), int):
                lines.append(
                    f"  Stored/declared components: {composite_summary['component_count']}"
                )
            if isinstance(membership, dict):
                total = membership.get("total_components")
                stored = membership.get("stored_components")
                if isinstance(total, int) and isinstance(stored, int):
                    lines.append(f"  Membership index: stored={stored}, total={total}")

            if components:
                lines.append(f"  Components (showing {len(components)}):")
                for component in components:
                    rel = component.get("relative_path", "?")
                    digest = str(component.get("component_digest", ""))[:16]
                    kind_name = component.get("leaf_kind", "file")
                    lines.append(f"    {rel} [{kind_name}] {digest}")

        # Dataset label (from artifact metadata)
        raw_meta = artifact.get("metadata")
        parsed_meta = None
        if isinstance(raw_meta, str):
            try:
                parsed_meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                parsed_meta = None
        elif isinstance(raw_meta, dict):
            parsed_meta = raw_meta

        if isinstance(parsed_meta, dict) and "dataset" in parsed_meta:
            ds = parsed_meta["dataset"]
            lines.append("\nDataset:")
            if ds.get("dataset_id"):
                lines.append(f"  ID: {ds['dataset_id']}")
            if ds.get("dataset_fingerprint"):
                algo = ds.get("dataset_fingerprint_algorithm", "")
                lines.append(f"  Fingerprint: {algo}:{ds['dataset_fingerprint'][:16]}...")
            if isinstance(ds.get("confidence"), (int, float)):
                lines.append(f"  Confidence: {ds['confidence']:.2f}")
            if ds.get("evidence"):
                lines.append(f"  Evidence: {', '.join(ds['evidence'])}")
            if ds.get("split"):
                lines.append(f"  Split: {ds['split']}")
            if ds.get("version_hint"):
                lines.append(f"  Version: {ds['version_hint']}")

        return "\n".join(lines)
