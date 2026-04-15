"""Pure comparison helpers for `roar diff`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .diff_graph import JobMatch, JobNode, LineageGraph
from .results import AtomicDiff, ChangeType, DiffCategory

_SEVERITY_WEIGHTS: dict[ChangeType, float] = {
    ChangeType.CONTENT_CHANGED: 1.0,
    ChangeType.PARAM_CHANGED: 0.9,
    ChangeType.CODE_CHANGED: 0.85,
    ChangeType.ENV_CHANGED: 0.5,
    ChangeType.ADDED: 0.7,
    ChangeType.REMOVED: 0.7,
}


@dataclass(frozen=True)
class GraphDiffComputation:
    """Computed diff state for two lineage graphs before rendering."""

    diffs: list[AtomicDiff]
    matched_jobs: list[JobMatch]
    only_in_a: list[JobNode]
    only_in_b: list[JobNode]
    root_cause: AtomicDiff | None = None


def compare_lineage_graphs(
    graph_a: LineageGraph,
    graph_b: LineageGraph,
) -> GraphDiffComputation:
    """Compare two lineage graphs and return the full computed diff state."""
    matched, only_in_a, only_in_b = match_jobs(graph_a, graph_b)
    diffs = diff_matched_jobs(matched)

    for job in only_in_a:
        step = f"@{job.step_number}" if job.step_number else job.job_uid
        diffs.append(
            AtomicDiff(
                change_type=ChangeType.REMOVED,
                category=DiffCategory.PIPELINE,
                description=f"Step {step} only in A: {job.command}",
                detail={"step": step, "command": job.command},
            )
        )
    for job in only_in_b:
        step = f"@{job.step_number}" if job.step_number else job.job_uid
        diffs.append(
            AtomicDiff(
                change_type=ChangeType.ADDED,
                category=DiffCategory.PIPELINE,
                description=f"Step {step} only in B: {job.command}",
                detail={"step": step, "command": job.command},
            )
        )

    scored_diffs = score_diffs(diffs, graph_a, graph_b)
    root_cause = next((diff for diff in scored_diffs if diff.is_root_cause), None)

    return GraphDiffComputation(
        diffs=scored_diffs,
        matched_jobs=matched,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
        root_cause=root_cause,
    )


def extract_script_and_args(command: str) -> tuple[str, list[str]]:
    """Extract the script name and trailing args from a command string."""
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    script = ""
    args: list[str] = []
    found_script = False
    for part in parts:
        if not found_script and part.endswith(".py"):
            script = os.path.basename(part)
            found_script = True
        elif found_script:
            args.append(part)
        elif not found_script and part not in ("python", "python3"):
            script = os.path.basename(part)
            found_script = True
    return script, args


def match_jobs(
    graph_a: LineageGraph,
    graph_b: LineageGraph,
) -> tuple[list[JobMatch], list[JobNode], list[JobNode]]:
    """Match jobs between two lineage graphs by command, then by script name."""
    matched: list[JobMatch] = []
    used_b: set[int] = set()

    for job_a in graph_a.jobs:
        for job_b in graph_b.jobs:
            if job_b.job_id in used_b:
                continue
            if job_a.command == job_b.command:
                matched.append(JobMatch(job_a, job_b))
                used_b.add(job_b.job_id)
                break

    matched_a_ids = {match.job_a.job_id for match in matched}

    for job_a in graph_a.jobs:
        if job_a.job_id in matched_a_ids:
            continue
        script_a, _ = extract_script_and_args(job_a.command)
        for job_b in graph_b.jobs:
            if job_b.job_id in used_b:
                continue
            script_b, _ = extract_script_and_args(job_b.command)
            if script_a and script_a == script_b:
                matched.append(JobMatch(job_a, job_b))
                used_b.add(job_b.job_id)
                matched_a_ids.add(job_a.job_id)
                break

    only_in_a = [job for job in graph_a.jobs if job.job_id not in matched_a_ids]
    only_in_b = [job for job in graph_b.jobs if job.job_id not in used_b]
    return matched, only_in_a, only_in_b


def diff_args(cmd_a: str, cmd_b: str) -> list[dict[str, str]]:
    """Compute argument-level diffs between two commands."""
    _, args_a = extract_script_and_args(cmd_a)
    _, args_b = extract_script_and_args(cmd_b)

    diffs: list[dict[str, str]] = []
    kv_a = parse_kv_args(args_a)
    kv_b = parse_kv_args(args_b)

    all_keys = set(kv_a.keys()) | set(kv_b.keys())
    for key in sorted(all_keys):
        val_a = kv_a.get(key)
        val_b = kv_b.get(key)
        if val_a != val_b:
            diffs.append({"param": key, "old": val_a or "(absent)", "new": val_b or "(absent)"})

    return diffs


def parse_kv_args(args: list[str]) -> dict[str, str]:
    """Parse ``--key value`` or ``--key=value`` args into a mapping."""
    result: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            key, val = arg.split("=", 1)
            result[key] = val
        elif arg.startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("-"):
            result[arg] = args[i + 1]
            i += 1
        elif arg.startswith("--"):
            result[arg] = ""
        i += 1
    return result


def diff_matched_jobs(matched: list[JobMatch]) -> list[AtomicDiff]:
    """Compute atomic diffs for matched job pairs."""
    diffs: list[AtomicDiff] = []

    for match in matched:
        job_a, job_b = match.job_a, match.job_b
        step_label = f"@{job_a.step_number}" if job_a.step_number else job_a.job_uid

        if job_a.command != job_b.command:
            arg_diffs = diff_args(job_a.command, job_b.command)
            if arg_diffs:
                for arg_diff in arg_diffs:
                    diffs.append(
                        AtomicDiff(
                            change_type=ChangeType.PARAM_CHANGED,
                            category=DiffCategory.PARAMS,
                            description=f"Step {step_label}: {arg_diff['param']}: {arg_diff['old']} -> {arg_diff['new']}",
                            detail={"step": step_label, **arg_diff},
                        )
                    )
            else:
                command_a = truncate_cmd(job_a.command, 60)
                command_b = truncate_cmd(job_b.command, 60)
                diffs.append(
                    AtomicDiff(
                        change_type=ChangeType.PARAM_CHANGED,
                        category=DiffCategory.PARAMS,
                        description=f"Step {step_label}: command differs\n      A: {command_a}\n      B: {command_b}",
                        detail={"step": step_label, "old": job_a.command, "new": job_b.command},
                    )
                )

        if job_a.git_commit and job_b.git_commit and job_a.git_commit != job_b.git_commit:
            diffs.append(
                AtomicDiff(
                    change_type=ChangeType.CODE_CHANGED,
                    category=DiffCategory.CODE,
                    description=f"Step {step_label}: code changed ({job_a.git_commit[:8]} -> {job_b.git_commit[:8]})",
                    detail={
                        "step": step_label,
                        "old_commit": job_a.git_commit,
                        "new_commit": job_b.git_commit,
                    },
                )
            )

        paths_a = {
            os.path.basename(path): (artifact_id, digest)
            for artifact_id, (path, digest) in zip_path_hash(
                job_a.input_paths, job_a.input_hashes
            ).items()
        }
        paths_b = {
            os.path.basename(path): (artifact_id, digest)
            for artifact_id, (path, digest) in zip_path_hash(
                job_b.input_paths, job_b.input_hashes
            ).items()
        }

        all_input_names = set(paths_a.keys()) | set(paths_b.keys())
        for name in sorted(all_input_names):
            entry_a = paths_a.get(name)
            entry_b = paths_b.get(name)
            if entry_a and entry_b:
                _, hash_a = entry_a
                _, hash_b = entry_b
                if hash_a and hash_b and hash_a != hash_b:
                    diffs.append(
                        AtomicDiff(
                            change_type=ChangeType.CONTENT_CHANGED,
                            category=DiffCategory.DATA,
                            description=f"Step {step_label}: input '{name}' has different content",
                            detail={
                                "step": step_label,
                                "input": name,
                                "hash_a": hash_a[:12],
                                "hash_b": hash_b[:12],
                            },
                        )
                    )
            elif entry_a and not entry_b:
                diffs.append(
                    AtomicDiff(
                        change_type=ChangeType.REMOVED,
                        category=DiffCategory.DATA,
                        description=f"Step {step_label}: input '{name}' not present in B",
                        detail={"step": step_label, "input": name},
                    )
                )
            elif entry_b and not entry_a:
                diffs.append(
                    AtomicDiff(
                        change_type=ChangeType.ADDED,
                        category=DiffCategory.DATA,
                        description=f"Step {step_label}: input '{name}' not present in A",
                        detail={"step": step_label, "input": name},
                    )
                )

        if not all_input_names and job_a.input_hashes and job_b.input_hashes:
            hashes_a = set(job_a.input_hashes.values())
            hashes_b = set(job_b.input_hashes.values())
            if hashes_a != hashes_b:
                diffs.append(
                    AtomicDiff(
                        change_type=ChangeType.CONTENT_CHANGED,
                        category=DiffCategory.DATA,
                        description=f"Step {step_label}: different input artifacts",
                        detail={"step": step_label},
                    )
                )

        only_a_names = sorted(set(paths_a.keys()) - set(paths_b.keys()))
        only_b_names = sorted(set(paths_b.keys()) - set(paths_a.keys()))
        if only_a_names and only_b_names and len(only_a_names) == len(only_b_names):
            removable = set(only_a_names) | set(only_b_names)
            diffs[:] = [
                diff
                for diff in diffs
                if not (
                    diff.detail.get("step") == step_label
                    and diff.category == DiffCategory.DATA
                    and diff.change_type in (ChangeType.ADDED, ChangeType.REMOVED)
                    and diff.detail.get("input") in removable
                )
            ]
            for name_a, name_b in zip(only_a_names, only_b_names, strict=True):
                _, hash_a = paths_a[name_a]
                _, hash_b = paths_b[name_b]
                diffs.append(
                    AtomicDiff(
                        change_type=ChangeType.CONTENT_CHANGED,
                        category=DiffCategory.DATA,
                        description=f"Step {step_label}: input '{name_a}' vs '{name_b}' (different data)",
                        detail={
                            "step": step_label,
                            "input_a": name_a,
                            "input_b": name_b,
                            "hash_a": (hash_a or "")[:12],
                            "hash_b": (hash_b or "")[:12],
                        },
                    )
                )

        diff_env(job_a, job_b, step_label, diffs)

    return diffs


def zip_path_hash(
    paths: dict[str, str],
    hashes: dict[str, str],
) -> dict[str, tuple[str, str | None]]:
    """Combine path and hash maps by artifact id."""
    result: dict[str, tuple[str, str | None]] = {}
    for artifact_id, path in paths.items():
        result[artifact_id] = (path, hashes.get(artifact_id))
    return result


def truncate_cmd(command: str, max_len: int) -> str:
    """Shorten a long command for human-readable diff output."""
    if len(command) <= max_len:
        return command
    return command[: max_len - 3] + "..."


def diff_env(job_a: JobNode, job_b: JobNode, step_label: str, diffs: list[AtomicDiff]) -> None:
    """Compare environment metadata between two jobs."""
    meta_a = job_a.metadata or {}
    meta_b = job_b.metadata or {}

    packages_a = extract_packages(meta_a)
    packages_b = extract_packages(meta_b)
    if packages_a and packages_b and packages_a != packages_b:
        changed = []
        for package in sorted(set(packages_a.keys()) | set(packages_b.keys())):
            version_a = packages_a.get(package)
            version_b = packages_b.get(package)
            if version_a != version_b:
                changed.append(f"{package}: {version_a or '(absent)'} -> {version_b or '(absent)'}")
        if changed:
            changed.sort(key=lambda value: (1 if "(absent)" in value else 0, value))
            preview = changed[:5]
            suffix = f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""
            detail_lines = "\n".join(f"      {line}" for line in preview)
            diffs.append(
                AtomicDiff(
                    change_type=ChangeType.ENV_CHANGED,
                    category=DiffCategory.COMPUTE,
                    description=f"Step {step_label}: {len(changed)} package(s) differ{suffix}\n{detail_lines}",
                    detail={"step": step_label, "packages": changed},
                )
            )


def extract_packages(metadata: dict[str, Any]) -> dict[str, str]:
    """Extract pip package versions from job metadata."""
    result: dict[str, str] = {}
    pip_packages = metadata.get("pip_packages") or metadata.get("packages", {}).get("pip", {})
    if isinstance(pip_packages, dict):
        for name, version in pip_packages.items():
            result[name] = str(version)
    elif isinstance(pip_packages, list):
        for entry in pip_packages:
            if isinstance(entry, str) and "==" in entry:
                name, version = entry.split("==", 1)
                result[name] = version
            elif isinstance(entry, dict):
                result[entry.get("name", "")] = entry.get("version", "")
    return result


def score_diffs(
    diffs: list[AtomicDiff],
    graph_a: LineageGraph,
    graph_b: LineageGraph,
) -> list[AtomicDiff]:
    """Score each diff by severity and proximity to likely root cause steps."""
    if not diffs:
        return diffs

    all_steps_a = {job.step_number for job in graph_a.jobs if job.step_number}
    all_steps_b = {job.step_number for job in graph_b.jobs if job.step_number}
    max_step = max(max(all_steps_a, default=1), max(all_steps_b, default=1))

    scored: list[AtomicDiff] = []
    for diff in diffs:
        severity = _SEVERITY_WEIGHTS.get(diff.change_type, 0.5)
        step_num = parse_step_number(diff.detail.get("step", ""))

        if step_num is not None and max_step > 0:
            proximity = 1.0 - (0.7 * (step_num - 1) / max_step)
        else:
            proximity = 0.5

        impact = round(severity * proximity, 3)
        scored.append(
            AtomicDiff(
                change_type=diff.change_type,
                category=diff.category,
                description=diff.description,
                detail=diff.detail,
                impact=impact,
                is_root_cause=False,
            )
        )

    scored.sort(key=lambda diff: -diff.impact)
    if scored:
        root = scored[0]
        scored[0] = AtomicDiff(
            change_type=root.change_type,
            category=root.category,
            description=root.description,
            detail=root.detail,
            impact=root.impact,
            is_root_cause=True,
        )

    return scored


def parse_step_number(step_str: str) -> int | None:
    """Extract a numeric step from an ``@N`` step label."""
    if step_str.startswith("@"):
        try:
            return int(step_str[1:])
        except ValueError:
            pass
    return None
