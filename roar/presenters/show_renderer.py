"""
Show command renderer for displaying session, job, and artifact details.

Handles all output formatting for the show command.
Follows SRP: only handles presentation, not data fetching.
"""

from __future__ import annotations

import json

from ..application.label_rendering import render_label_lines
from ..core.step_name import omit_step_name_label
from .formatting import format_duration, format_size, format_timestamp


def _short_hash_label(hashes: list[dict] | None, *, prefix_len: int = 12) -> str:
    """Compact `algo:hash…` label for inline use in artifact rows.

    Picks the first recorded hash (blake3 is roar's default, so it
    almost always leads). Returns just `?` if no hash is recorded —
    rare, but keeps the column shape consistent.
    """
    if not hashes:
        return "?"
    first = hashes[0]
    algo = str(first.get("algorithm") or "?")
    digest = str(first.get("digest") or "")
    return f"{algo}:{digest[:prefix_len]}…"


class ShowRenderer:
    """
    Renders show command output as plain text strings.

    All methods accept pre-fetched data and return formatted strings,
    keeping presentation separate from data access.
    """

    def __init__(self, *, show_all: bool = False) -> None:
        self.show_all = show_all

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
            if not self.show_all and len(command) > 50:
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

        # ---- header block ----------------------------------------------------
        # One-line summary: step ref · UID · exit · duration · timestamp.
        # Exit code does the work that the old "Status: Success" row did —
        # zero is silent, nonzero is loud.
        step_ref = "@?"
        if job.get("step_number"):
            prefix = "@B" if job.get("job_type") == "build" else "@"
            step_ref = f"{prefix}{job['step_number']}"

        header_parts = [step_ref, str(job.get("job_uid") or "?")]
        exit_code = job.get("exit_code")
        if exit_code is None:
            header_parts.append("exit ?")
        elif exit_code != 0:
            header_parts.append(f"exit {exit_code}")
        else:
            header_parts.append("exit 0")
        header_parts.append(format_duration(job.get("duration_seconds")))
        header_parts.append(format_timestamp(job.get("timestamp")))
        lines.append("Job:       " + "  ·  ".join(header_parts))

        if job.get("step_name"):
            lines.append(f"Name:      {job['step_name']}")
        if job.get("step_identity"):
            lines.append(f"Identity:  {job['step_identity']}")

        source_parts: list[str] = []
        if job.get("git_branch"):
            source_parts.append(str(job["git_branch"]))
        if job.get("git_commit"):
            commit = str(job["git_commit"])
            source_parts.append(f"@ {commit[:7]}" if source_parts else commit)
        if source_parts:
            lines.append("Source:    " + " ".join(source_parts))

        self._render_labels(lines, omit_step_name_label(labels, step_name=job.get("step_name")))

        if job.get("command"):
            lines.append("")
            lines.append(f"  $ {job['command']}")

        # ---- inputs / outputs ------------------------------------------------
        if inputs:
            lines.append("")
            lines.append(f"Inputs ({len(inputs)}):")
            lines.extend(self._render_job_artifact_rows(inputs))

        if outputs:
            lines.append("")
            lines.append(f"Outputs ({len(outputs)}):")
            lines.extend(self._render_job_artifact_rows(outputs))

        # ---- environment summary --------------------------------------------
        # By default we collapse the full metadata block into a 2-line
        # summary (host facts + package counts). `--all` restores the
        # exhaustive listing for repro debugging.
        meta = job.get("metadata")
        if isinstance(meta, dict):
            if self.show_all:
                lines.extend(self._render_full_metadata(meta))
            else:
                summary_lines = self._render_environment_summary(meta)
                if summary_lines:
                    lines.append("")
                    lines.append("Environment:")
                    lines.extend(summary_lines)

        # ---- telemetry (external links) --------------------------------------
        telem = job.get("telemetry")
        if telem and isinstance(telem, dict):
            lines.append("")
            lines.append("Telemetry:")
            for name, url in telem.items():
                if isinstance(url, list):
                    for u in url:
                        lines.append(f"  {name}: {u}")
                else:
                    lines.append(f"  {name}: {url}")

        return "\n".join(lines)

    def _render_job_artifact_rows(self, artifacts: list[dict]) -> list[str]:
        """One line per artifact: `algo:hash…  SIZE  /path` plus a
        `(composite, N components)` annotation when non-primitive."""
        out: list[str] = []
        for art in artifacts:
            hashes = art.get("hashes") or []
            hash_label = _short_hash_label(hashes)
            size = format_size(art.get("size"))
            path = art.get("path") or "?"
            out.append(f"  {hash_label:<22} {size:>9}  {path}")
            kind = art.get("kind")
            component_count = art.get("component_count")
            if isinstance(kind, str) and kind != "primitive":
                if kind == "composite" and isinstance(component_count, int):
                    out.append(f"    ({kind}, {component_count} components)")
                else:
                    out.append(f"    ({kind})")
        return out

    @staticmethod
    def _render_environment_summary(meta: dict) -> list[str]:
        """Two-line summary of the metadata block for the default view."""
        runtime = meta.get("runtime") or {}
        host_bits: list[str] = []
        if runtime.get("hostname"):
            host_bits.append(str(runtime["hostname"]))
        os_info = runtime.get("os") or {}
        if os_info.get("system"):
            host_bits.append(str(os_info["system"]))
        python_info = runtime.get("python") or {}
        if python_info.get("version"):
            host_bits.append(f"Python {python_info['version']}")
        cpu = runtime.get("cpu") or {}
        cpu_count = cpu.get("count")
        if isinstance(cpu_count, int):
            host_bits.append(f"{cpu_count} cpu")
        memory = runtime.get("memory") or {}
        mem_mb = memory.get("total_mb")
        if isinstance(mem_mb, (int, float)) and mem_mb > 0:
            host_bits.append(f"{mem_mb / 1024:.1f} GB")

        pkg_bits: list[str] = []
        packages = meta.get("packages")
        if isinstance(packages, dict):
            for manager, pkgs in packages.items():
                if isinstance(pkgs, dict) and pkgs:
                    pkg_bits.append(f"{len(pkgs)} {manager}")
        env_vars = runtime.get("env_vars")
        if isinstance(env_vars, dict) and env_vars:
            pkg_bits.append(f"{len(env_vars)} env vars set")

        out: list[str] = []
        if host_bits:
            out.append(f"  host:     {' · '.join(host_bits)}")
        if pkg_bits:
            out.append(f"  packages: {' · '.join(pkg_bits)}    (--all for full lists)")
        return out

    def _render_full_metadata(self, meta: dict) -> list[str]:
        """Full pre-overhaul metadata block — kept for `--all`."""
        lines: list[str] = ["", "Metadata:"]

        if meta.get("cwd"):
            lines.append(f"  Working dir: {meta['cwd']}")

        runtime = meta.get("runtime", {}) or {}
        if runtime.get("hostname"):
            lines.append(f"  Hostname: {runtime['hostname']}")
        if runtime.get("os"):
            os_info = runtime["os"]
            lines.append(f"  OS: {os_info.get('system', '')} {os_info.get('release', '')}")
        if runtime.get("python"):
            lines.append(f"  Python: {runtime['python'].get('version', '')}")

        if runtime.get("gpu"):
            for i, gpu in enumerate(runtime["gpu"]):
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
            lines.append(f"  CPU: {cpu.get('model', 'unknown')} ({cpu.get('count', '?')} cores)")
        if runtime.get("memory"):
            mem = runtime["memory"]
            lines.append(f"  Memory: {mem.get('total_mb', '?')} MB")

        env_vars = runtime.get("env_vars") or {}
        if env_vars:
            lines.append(f"\n  Environment Variables ({len(env_vars)}):")
            for name, value in sorted(env_vars.items()):
                lines.append(f"    {name}={value}")

        packages = meta.get("packages")
        if isinstance(packages, dict):
            for manager, pkgs in packages.items():
                if isinstance(pkgs, dict) and pkgs:
                    lines.append(f"\n  Packages ({manager}, {len(pkgs)}):")
                    for name, version in sorted(pkgs.items()):
                        if version:
                            lines.append(f"    {name}=={version}")
                        else:
                            lines.append(f"    {name}")

        return lines

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

        # ---- header: hash (with algo label) + b3sum breadcrumb for blake3 ----
        hashes = artifact.get("hashes") or []
        first_seen_path = artifact.get("first_seen_path") or ""
        if len(hashes) == 1:
            algo = str(hashes[0].get("algorithm") or "?")
            digest = str(hashes[0].get("digest") or "")
            lines.append(f"Hash ({algo}):  {digest}")
            # blake3 specifically matches `b3sum <file>`. Give the user
            # the breadcrumb so they can verify or look it up later.
            if algo == "blake3" and first_seen_path:
                lines.append(f"                ≡ b3sum {first_seen_path}")
        elif hashes:
            lines.append("Hashes:")
            for h in hashes:
                algo = str(h.get("algorithm") or "?")
                digest = str(h.get("digest") or "")
                lines.append(f"  {algo}: {digest}")
                if algo == "blake3" and first_seen_path:
                    lines.append(f"    ≡ b3sum {first_seen_path}")
        else:
            # Fall back to the internal id only when no content hash is
            # recorded — that's the only remaining identifier.
            lines.append(f"Artifact: {artifact.get('id', '?')}")

        if artifact.get("source") == "remote":
            lines.append("Source:     GLaaS")
        kind = artifact.get("kind")
        component_count = artifact.get("component_count")
        if isinstance(kind, str) and kind != "primitive":
            if kind == "composite" and isinstance(component_count, int):
                lines.append(f"Kind:       {kind} ({component_count} components)")
            else:
                lines.append(f"Kind:       {kind}")
        lines.append(f"Size:       {format_size(artifact['size'])}")
        if first_seen_path:
            lines.append(f"Path:       {first_seen_path}")
        lines.append(f"First seen: {format_timestamp(artifact['first_seen_at'])}")

        self._render_labels(lines, labels)

        # ---- locations -------------------------------------------------------
        # Skip when the single location matches the first-seen path —
        # showing it twice is noise. Surface only the additional ones.
        extra_locations = [loc for loc in locations if (loc.get("path") or "") != first_seen_path]
        if extra_locations:
            lines.append("")
            lines.append(f"Also located at ({len(extra_locations)}):")
            for loc in extra_locations:
                lines.append(f"  {loc['path']}")

        # ---- jobs: producers / consumers -------------------------------------
        for label, key in (("Produced by", "produced_by"), ("Consumed by", "consumed_by")):
            entries = jobs.get(key) or []
            if not entries:
                continue
            lines.append("")
            commands = {(entry.get("command") or "") for entry in entries}
            if len(entries) > 1 and len(commands) == 1:
                lines.append(f"{label} ({len(entries)} runs of the same step):")
            else:
                noun = "job" if len(entries) == 1 else "jobs"
                lines.append(f"{label} ({len(entries)} {noun}):")
            visible = entries if self.show_all else entries[:3]
            for entry in visible:
                uid = str(entry.get("job_uid") or "?")
                cmd = entry.get("command") or "?"
                cmd_short = cmd if len(cmd) <= 60 else cmd[:59] + "…"
                lines.append(f"  {uid}  {cmd_short}")
            if not self.show_all and len(entries) > len(visible):
                lines.append(f"  + {len(entries) - len(visible)} more  (use --all)")

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
