"""Application orchestration for the roar diff command."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ...core.digests import extract_primary_digest
from ...db.query_context import QueryDatabaseContext, create_query_database_context
from .requests import DiffQueryRequest


class DiffError(RuntimeError):
    """Raised when a diff query cannot be completed."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChangeType(str, Enum):
    CONTENT_CHANGED = "content_changed"
    PARAM_CHANGED = "param_changed"
    CODE_CHANGED = "code_changed"
    ENV_CHANGED = "env_changed"
    ADDED = "added"
    REMOVED = "removed"


class DiffCategory(str, Enum):
    DATA = "data"
    CODE = "code"
    PARAMS = "params"
    COMPUTE = "compute"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class AtomicDiff:
    change_type: ChangeType
    category: DiffCategory
    description: str
    detail: dict[str, Any] = field(default_factory=dict)
    impact: float = 0.0  # Phase 4: 0.0-1.0
    is_root_cause: bool = False  # Phase 4


@dataclass(frozen=True)
class JobNode:
    """A job in the lineage graph."""

    job_id: int
    job_uid: str
    command: str
    step_number: int | None
    git_commit: str | None
    git_branch: str | None
    metadata: dict[str, Any] | None
    input_hashes: dict[str, str]  # artifact_id -> primary digest
    output_hashes: dict[str, str]  # artifact_id -> primary digest
    input_paths: dict[str, str]  # artifact_id -> path
    output_paths: dict[str, str]  # artifact_id -> path


@dataclass(frozen=True)
class LineageGraph:
    """A normalized lineage graph for one target."""

    target_artifact_id: str
    target_hash: str | None
    target_path: str | None
    jobs: list[JobNode]  # topologically sorted (earliest first)


@dataclass
class JobMatch:
    job_a: JobNode
    job_b: JobNode


@dataclass
class DiffResult:
    ref_a: str
    ref_b: str
    target_a_hash: str | None
    target_b_hash: str | None
    target_a_path: str | None
    target_b_path: str | None
    targets_identical: bool
    diffs: list[AtomicDiff]
    matched_jobs: list[JobMatch]
    only_in_a: list[JobNode]
    only_in_b: list[JobNode]
    root_cause: AtomicDiff | None = None  # Phase 4
    analysis: str | None = None  # Phase 5: LLM narrative


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------


_GLAAS_PREFIX = "glaas:"
_SESSION_PREFIX = "session:"


def _classify_ref(ref: str) -> str:
    if ref.startswith(_GLAAS_PREFIX):
        return "glaas"
    if ref.startswith(_SESSION_PREFIX):
        return "session"
    if ref.startswith("@"):
        return "job_step"
    if "/" in ref or ref.startswith(("./", "../", "~")):
        return "file_path"
    is_hex = bool(ref) and all(c in "0123456789abcdefABCDEF" for c in ref)
    if is_hex and len(ref) <= 8:
        return "job_uid"
    if is_hex and len(ref) > 8:
        return "artifact_hash"
    return "path_candidate"


def _resolve_ref_to_artifact_id(
    db_ctx: QueryDatabaseContext, ref: str, cwd: Path
) -> tuple[str, str | None]:
    """Resolve a reference to (artifact_id, display_path).

    For job refs (@N, uid), returns the first output artifact.
    For artifact refs (hash, path), returns the artifact directly.
    """
    ref_type = _classify_ref(ref)

    if ref_type == "job_step":
        return _resolve_job_ref_to_artifact(db_ctx, ref)

    if ref_type == "job_uid":
        job = db_ctx.jobs.get_by_uid(ref)
        if job:
            outputs = db_ctx.jobs.get_outputs(job["id"])
            if outputs:
                return str(outputs[0]["artifact_id"]), outputs[0].get("path")
        # Fall through to artifact hash
        artifact = db_ctx.artifacts.get_by_hash(ref)
        if artifact:
            return str(artifact["id"]), artifact.get("first_seen_path")
        raise DiffError(f"Not found: {ref}")

    if ref_type == "file_path":
        return _resolve_path_to_artifact(db_ctx, ref, cwd)

    if ref_type == "artifact_hash":
        # Try as job UID first (like show does)
        job = db_ctx.jobs.get_by_uid(ref)
        if job:
            outputs = db_ctx.jobs.get_outputs(job["id"])
            if outputs:
                return str(outputs[0]["artifact_id"]), outputs[0].get("path")
        artifact = db_ctx.artifacts.get_by_hash(ref)
        if artifact:
            return str(artifact["id"]), artifact.get("first_seen_path")
        raise DiffError(f"Artifact not found: {ref}")

    # path_candidate
    return _resolve_path_to_artifact(db_ctx, ref, cwd)


def _resolve_job_ref_to_artifact(
    db_ctx: QueryDatabaseContext, ref: str
) -> tuple[str, str | None]:
    session = db_ctx.sessions.get_active()
    if not session:
        raise DiffError("No active session.")
    step_ref = ref[1:]  # strip @
    job_type = None
    if step_ref.startswith("B"):
        job_type = "build"
        step_ref = step_ref[1:]
    try:
        step_number = int(step_ref)
    except ValueError:
        raise DiffError(f"Invalid step reference: {ref}")
    job = db_ctx.sessions.get_step_by_number(int(session["id"]), step_number, job_type)
    if not job:
        raise DiffError(f"Step not found: {ref}")
    outputs = db_ctx.jobs.get_outputs(job["id"])
    if not outputs:
        raise DiffError(f"Step {ref} has no output artifacts.")
    return str(outputs[0]["artifact_id"]), outputs[0].get("path")


def _resolve_path_to_artifact(
    db_ctx: QueryDatabaseContext, ref: str, cwd: Path
) -> tuple[str, str | None]:
    path_obj = Path(os.path.expanduser(ref))
    if not path_obj.is_absolute():
        path_obj = cwd / path_obj
    resolved_path = os.path.normpath(str(path_obj.absolute()))
    artifact = db_ctx.artifacts.get_by_path(resolved_path)
    if not artifact:
        raise DiffError(f"No artifact found for path: {ref}")
    return str(artifact["id"]), resolved_path


# ---------------------------------------------------------------------------
# Phase 2: Session resolution
# ---------------------------------------------------------------------------


def _resolve_session_ref(db_ctx: QueryDatabaseContext, ref: str) -> dict[str, Any]:
    """Resolve a session: ref to a session dict."""
    session_key = ref[len(_SESSION_PREFIX):]
    if session_key == "current":
        session = db_ctx.sessions.get_active()
        if not session:
            raise DiffError("No active session.")
        return session

    # Try as session hash prefix
    row = db_ctx._fetchone(
        "SELECT * FROM sessions WHERE hash LIKE ? LIMIT 2",
        (f"{session_key}%",),
    )
    if row is None:
        raise DiffError(f"Session not found: {session_key}")
    return {
        "id": int(row["id"]),
        "hash": row["hash"],
        "created_at": row["created_at"],
        "git_repo": row["git_repo"],
        "git_commit_start": row["git_commit_start"],
        "git_commit_end": row["git_commit_end"],
    }


def _extract_session_lineage(
    db_ctx: QueryDatabaseContext, session: dict[str, Any]
) -> LineageGraph:
    """Build a LineageGraph from all jobs in a session."""
    steps = db_ctx.sessions.get_steps(int(session["id"]))
    job_nodes: list[JobNode] = []

    for step in steps:
        job_id = step["id"]
        inputs = db_ctx.jobs.get_inputs(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)

        input_hashes, input_paths = _collect_artifact_info(inputs)
        output_hashes, output_paths = _collect_artifact_info(outputs)
        metadata = _parse_metadata(step.get("metadata"))

        job_nodes.append(
            JobNode(
                job_id=job_id,
                job_uid=step.get("job_uid", ""),
                command=step.get("command", ""),
                step_number=step.get("step_number"),
                git_commit=step.get("git_commit"),
                git_branch=step.get("git_branch"),
                metadata=metadata,
                input_hashes=input_hashes,
                output_hashes=output_hashes,
                input_paths=input_paths,
                output_paths=output_paths,
            )
        )

    job_nodes.sort(key=lambda j: (j.step_number or 0, j.job_id))

    # For session diffs, use the session hash as the "target"
    return LineageGraph(
        target_artifact_id=f"session:{session['hash'] or session['id']}",
        target_hash=session.get("hash"),
        target_path=None,
        jobs=job_nodes,
    )


# ---------------------------------------------------------------------------
# Phase 3: GLaaS remote resolution
# ---------------------------------------------------------------------------


def _resolve_glaas_ref(ref: str) -> LineageGraph:
    """Resolve a glaas: reference to a LineageGraph by querying the GLaaS server."""
    glaas_key = ref[len(_GLAAS_PREFIX):]

    try:
        from ...integrations.glaas.client import GlaasClient
    except ImportError:
        raise DiffError("GLaaS client not available.")

    client = GlaasClient()
    if not client.is_configured():
        raise DiffError(
            "GLaaS URL not configured. Run 'roar config set glaas.url <url>'."
        )

    # Try artifact DAG first (gives us full lineage)
    result, error = client.get_artifact_dag(glaas_key)
    if error:
        # Fall back to lineage endpoint
        result, error = client.get_artifact_lineage(glaas_key, depth=10)
        if error:
            raise DiffError(f"GLaaS lookup failed for '{glaas_key}': {error}")

    return _glaas_payload_to_graph(glaas_key, result)


def _glaas_payload_to_graph(ref_key: str, payload: dict[str, Any] | None) -> LineageGraph:
    """Convert a GLaaS API response into a LineageGraph."""
    if not payload:
        raise DiffError(f"Empty response from GLaaS for '{ref_key}'.")

    job_nodes: list[JobNode] = []
    target_hash: str | None = None
    target_path: str | None = None

    # Extract artifact info
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        hashes = artifact.get("hashes", [])
        for h in hashes:
            if isinstance(h, dict) and h.get("algorithm") == "blake3":
                target_hash = h["digest"]
                break
        if not target_hash and hashes:
            target_hash = hashes[0].get("digest") if isinstance(hashes[0], dict) else None
        target_path = artifact.get("first_seen_path") or artifact.get("path")

    # Extract jobs
    jobs_list = payload.get("jobs", [])
    for i, job_data in enumerate(jobs_list):
        if not isinstance(job_data, dict):
            continue

        input_hashes: dict[str, str] = {}
        input_paths: dict[str, str] = {}
        for inp in job_data.get("inputs", []):
            if isinstance(inp, dict):
                art_hash = inp.get("hash", inp.get("artifact_hash", ""))
                path = inp.get("path", "")
                if art_hash:
                    input_hashes[art_hash] = art_hash
                    input_paths[art_hash] = path

        output_hashes: dict[str, str] = {}
        output_paths: dict[str, str] = {}
        for out in job_data.get("outputs", []):
            if isinstance(out, dict):
                art_hash = out.get("hash", out.get("artifact_hash", ""))
                path = out.get("path", "")
                if art_hash:
                    output_hashes[art_hash] = art_hash
                    output_paths[art_hash] = path

        metadata = None
        raw_meta = job_data.get("metadata")
        if isinstance(raw_meta, str):
            try:
                metadata = json.loads(raw_meta)
            except json.JSONDecodeError:
                pass
        elif isinstance(raw_meta, dict):
            metadata = raw_meta

        job_nodes.append(
            JobNode(
                job_id=-(i + 1),  # negative IDs for remote jobs
                job_uid=job_data.get("job_uid", f"remote-{i}"),
                command=job_data.get("command", ""),
                step_number=job_data.get("step_number"),
                git_commit=job_data.get("git_commit"),
                git_branch=job_data.get("git_branch"),
                metadata=metadata,
                input_hashes=input_hashes,
                output_hashes=output_hashes,
                input_paths=input_paths,
                output_paths=output_paths,
            )
        )

    job_nodes.sort(key=lambda j: (j.step_number or 0, j.job_id))

    return LineageGraph(
        target_artifact_id=f"glaas:{ref_key}",
        target_hash=target_hash,
        target_path=target_path,
        jobs=job_nodes,
    )


# ---------------------------------------------------------------------------
# Lineage graph extraction (local)
# ---------------------------------------------------------------------------


def _collect_artifact_info(
    artifacts: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build hash and path dicts from a list of artifact dicts."""
    hashes: dict[str, str] = {}
    paths: dict[str, str] = {}
    for art in artifacts:
        aid = str(art["artifact_id"])
        h = _get_primary_hash(art)
        if h:
            hashes[aid] = h
        paths[aid] = art.get("path") or art.get("first_seen_path") or ""
    return hashes, paths


def _parse_metadata(raw: Any) -> dict[str, Any] | None:
    """Parse metadata from string or dict."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        return raw
    return None


def _extract_lineage(
    db_ctx: QueryDatabaseContext, artifact_id: str, display_path: str | None, max_depth: int
) -> LineageGraph:
    """Extract the upstream lineage graph for an artifact."""
    artifact = db_ctx.artifacts.get(artifact_id)
    target_hash = _get_primary_hash(artifact) if artifact else None

    visited_jobs: set[int] = set()
    visited_artifacts: set[str] = set()
    job_nodes: list[JobNode] = []

    def trace_upstream(art_id: str, depth: int) -> None:
        if depth > max_depth or art_id in visited_artifacts:
            return
        visited_artifacts.add(art_id)

        jobs_info = db_ctx.artifacts.get_jobs(art_id)
        produced_by = jobs_info.get("produced_by", [])
        producer = produced_by[0] if produced_by else None

        if producer and producer["id"] not in visited_jobs:
            visited_jobs.add(producer["id"])
            job_id = producer["id"]

            inputs = db_ctx.jobs.get_inputs(job_id)
            outputs = db_ctx.jobs.get_outputs(job_id)

            input_hashes, input_paths = _collect_artifact_info(inputs)
            output_hashes, output_paths = _collect_artifact_info(outputs)
            metadata = _parse_metadata(producer.get("metadata"))

            job_nodes.append(
                JobNode(
                    job_id=job_id,
                    job_uid=producer.get("job_uid", ""),
                    command=producer.get("command", ""),
                    step_number=producer.get("step_number"),
                    git_commit=producer.get("git_commit"),
                    git_branch=producer.get("git_branch"),
                    metadata=metadata,
                    input_hashes=input_hashes,
                    output_hashes=output_hashes,
                    input_paths=input_paths,
                    output_paths=output_paths,
                )
            )

            for inp in inputs:
                trace_upstream(inp["artifact_id"], depth + 1)

    trace_upstream(artifact_id, 0)
    job_nodes.sort(key=lambda j: (j.step_number or 0, j.job_id))

    return LineageGraph(
        target_artifact_id=artifact_id,
        target_hash=target_hash,
        target_path=display_path,
        jobs=job_nodes,
    )


def _get_primary_hash(item: dict[str, Any] | None) -> str | None:
    return extract_primary_digest(item)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _extract_script_and_args(command: str) -> tuple[str, list[str]]:
    """Extract the script name and arguments from a command string."""
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    # Find the script (first .py file, or the whole command)
    script = ""
    args = []
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


def _match_jobs(graph_a: LineageGraph, graph_b: LineageGraph) -> tuple[
    list[JobMatch], list[JobNode], list[JobNode]
]:
    """Match jobs between two lineage graphs.

    Strategy:
    1. Exact match: same command string
    2. Script match: same script name, different args
    """
    matched: list[JobMatch] = []
    used_b: set[int] = set()

    # Pass 1: exact command match
    for ja in graph_a.jobs:
        for jb in graph_b.jobs:
            if jb.job_id in used_b:
                continue
            if ja.command == jb.command:
                matched.append(JobMatch(ja, jb))
                used_b.add(jb.job_id)
                break

    matched_a_ids = {m.job_a.job_id for m in matched}

    # Pass 2: script name match for unmatched jobs
    for ja in graph_a.jobs:
        if ja.job_id in matched_a_ids:
            continue
        script_a, _ = _extract_script_and_args(ja.command)
        for jb in graph_b.jobs:
            if jb.job_id in used_b:
                continue
            script_b, _ = _extract_script_and_args(jb.command)
            if script_a and script_a == script_b:
                matched.append(JobMatch(ja, jb))
                used_b.add(jb.job_id)
                matched_a_ids.add(ja.job_id)
                break

    only_in_a = [j for j in graph_a.jobs if j.job_id not in matched_a_ids]
    only_in_b = [j for j in graph_b.jobs if j.job_id not in used_b]

    return matched, only_in_a, only_in_b


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


def _diff_args(cmd_a: str, cmd_b: str) -> list[dict[str, str]]:
    """Compute argument-level diffs between two commands."""
    _, args_a = _extract_script_and_args(cmd_a)
    _, args_b = _extract_script_and_args(cmd_b)

    diffs = []
    # Parse key-value args
    kv_a = _parse_kv_args(args_a)
    kv_b = _parse_kv_args(args_b)

    all_keys = set(kv_a.keys()) | set(kv_b.keys())
    for key in sorted(all_keys):
        val_a = kv_a.get(key)
        val_b = kv_b.get(key)
        if val_a != val_b:
            diffs.append({"param": key, "old": val_a or "(absent)", "new": val_b or "(absent)"})

    return diffs


def _parse_kv_args(args: list[str]) -> dict[str, str]:
    """Parse --key value or --key=value args into a dict.

    Positional (non-flag) arguments are ignored — they're typically file paths
    that are better compared via artifact hashes in the data diff.
    """
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
        # Skip positional args — they're compared via artifact hashes
        i += 1
    return result


def _diff_matched_jobs(matched: list[JobMatch]) -> list[AtomicDiff]:
    """Compute atomic diffs for each matched job pair."""
    diffs: list[AtomicDiff] = []

    for match in matched:
        ja, jb = match.job_a, match.job_b
        step_label = f"@{ja.step_number}" if ja.step_number else ja.job_uid

        # 1. Parameter diffs
        if ja.command != jb.command:
            arg_diffs = _diff_args(ja.command, jb.command)
            if arg_diffs:
                for ad in arg_diffs:
                    diffs.append(AtomicDiff(
                        change_type=ChangeType.PARAM_CHANGED,
                        category=DiffCategory.PARAMS,
                        description=f"Step {step_label}: {ad['param']}: {ad['old']} -> {ad['new']}",
                        detail={"step": step_label, **ad},
                    ))
            else:
                # Show truncated commands when no structured args differ
                cmd_a_short = _truncate_cmd(ja.command, 60)
                cmd_b_short = _truncate_cmd(jb.command, 60)
                diffs.append(AtomicDiff(
                    change_type=ChangeType.PARAM_CHANGED,
                    category=DiffCategory.PARAMS,
                    description=f"Step {step_label}: command differs\n      A: {cmd_a_short}\n      B: {cmd_b_short}",
                    detail={"step": step_label, "old": ja.command, "new": jb.command},
                ))

        # 2. Code diffs (git commit)
        if ja.git_commit and jb.git_commit and ja.git_commit != jb.git_commit:
            diffs.append(AtomicDiff(
                change_type=ChangeType.CODE_CHANGED,
                category=DiffCategory.CODE,
                description=f"Step {step_label}: code changed ({ja.git_commit[:8]} -> {jb.git_commit[:8]})",
                detail={"step": step_label, "old_commit": ja.git_commit, "new_commit": jb.git_commit},
            ))

        # 3. Input data diffs
        # Match inputs by path basename
        paths_a = {os.path.basename(p): (aid, h) for aid, (p, h) in
                    _zip_path_hash(ja.input_paths, ja.input_hashes).items()}
        paths_b = {os.path.basename(p): (aid, h) for aid, (p, h) in
                    _zip_path_hash(jb.input_paths, jb.input_hashes).items()}

        all_input_names = set(paths_a.keys()) | set(paths_b.keys())
        for name in sorted(all_input_names):
            entry_a = paths_a.get(name)
            entry_b = paths_b.get(name)
            if entry_a and entry_b:
                _, hash_a = entry_a
                _, hash_b = entry_b
                if hash_a and hash_b and hash_a != hash_b:
                    diffs.append(AtomicDiff(
                        change_type=ChangeType.CONTENT_CHANGED,
                        category=DiffCategory.DATA,
                        description=f"Step {step_label}: input '{name}' has different content",
                        detail={"step": step_label, "input": name, "hash_a": hash_a[:12], "hash_b": hash_b[:12]},
                    ))
            elif entry_a and not entry_b:
                diffs.append(AtomicDiff(
                    change_type=ChangeType.REMOVED,
                    category=DiffCategory.DATA,
                    description=f"Step {step_label}: input '{name}' not present in B",
                    detail={"step": step_label, "input": name},
                ))
            elif entry_b and not entry_a:
                diffs.append(AtomicDiff(
                    change_type=ChangeType.ADDED,
                    category=DiffCategory.DATA,
                    description=f"Step {step_label}: input '{name}' not present in A",
                    detail={"step": step_label, "input": name},
                ))

        # If inputs match by hash but different paths (different file, same content) — no diff
        # If scripts match but input filenames differ entirely, report it
        if not all_input_names and ja.input_hashes and jb.input_hashes:
            hashes_a = set(ja.input_hashes.values())
            hashes_b = set(jb.input_hashes.values())
            if hashes_a != hashes_b:
                diffs.append(AtomicDiff(
                    change_type=ChangeType.CONTENT_CHANGED,
                    category=DiffCategory.DATA,
                    description=f"Step {step_label}: different input artifacts",
                    detail={"step": step_label},
                ))

        # When inputs have different names, pair up the unmatched ones
        # positionally and replace the added/removed diffs with a clearer
        # "X vs Y" comparison.
        only_a_names = sorted(set(paths_a.keys()) - set(paths_b.keys()))
        only_b_names = sorted(set(paths_b.keys()) - set(paths_a.keys()))
        if only_a_names and only_b_names and len(only_a_names) == len(only_b_names):
            # Remove the per-name added/removed diffs we just appended
            removable = set(only_a_names) | set(only_b_names)
            diffs[:] = [d for d in diffs if not (
                d.detail.get("step") == step_label
                and d.category == DiffCategory.DATA
                and d.change_type in (ChangeType.ADDED, ChangeType.REMOVED)
                and d.detail.get("input") in removable
            )]
            for name_a, name_b in zip(only_a_names, only_b_names):
                _, hash_a = paths_a[name_a]
                _, hash_b = paths_b[name_b]
                diffs.append(AtomicDiff(
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
                ))

        # 4. Environment diffs
        _diff_env(ja, jb, step_label, diffs)

    return diffs


def _zip_path_hash(
    paths: dict[str, str], hashes: dict[str, str]
) -> dict[str, tuple[str, str | None]]:
    """Combine path and hash dicts by artifact_id."""
    result: dict[str, tuple[str, str | None]] = {}
    for aid, path in paths.items():
        result[aid] = (path, hashes.get(aid))
    return result


def _truncate_cmd(cmd: str, max_len: int) -> str:
    if len(cmd) <= max_len:
        return cmd
    return cmd[:max_len - 3] + "..."


def _diff_env(ja: JobNode, jb: JobNode, step_label: str, diffs: list[AtomicDiff]) -> None:
    """Compare environment metadata between two jobs."""
    meta_a = ja.metadata or {}
    meta_b = jb.metadata or {}

    # Package diffs
    pkgs_a = _extract_packages(meta_a)
    pkgs_b = _extract_packages(meta_b)
    if pkgs_a and pkgs_b and pkgs_a != pkgs_b:
        changed = []
        for pkg in sorted(set(pkgs_a.keys()) | set(pkgs_b.keys())):
            va = pkgs_a.get(pkg)
            vb = pkgs_b.get(pkg)
            if va != vb:
                changed.append(f"{pkg}: {va or '(absent)'} -> {vb or '(absent)'}")
        if changed:
            # Sort: version changes first, then added/removed
            changed.sort(key=lambda c: (1 if "(absent)" in c else 0, c))
            # Show up to 5 specific diffs in the description
            preview = changed[:5]
            suffix = f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""
            detail_lines = "\n".join(f"      {c}" for c in preview)
            diffs.append(AtomicDiff(
                change_type=ChangeType.ENV_CHANGED,
                category=DiffCategory.COMPUTE,
                description=f"Step {step_label}: {len(changed)} package(s) differ{suffix}\n{detail_lines}",
                detail={"step": step_label, "packages": changed},
            ))


def _extract_packages(metadata: dict[str, Any]) -> dict[str, str]:
    """Extract pip packages from metadata."""
    result: dict[str, str] = {}
    pip_packages = metadata.get("pip_packages") or metadata.get("packages", {}).get("pip", {})
    if isinstance(pip_packages, dict):
        # Format: {"package_name": "version", ...}
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


# ---------------------------------------------------------------------------
# Phase 4: Impact scoring and root cause detection
# ---------------------------------------------------------------------------


_SEVERITY_WEIGHTS: dict[ChangeType, float] = {
    ChangeType.CONTENT_CHANGED: 1.0,
    ChangeType.PARAM_CHANGED: 0.9,
    ChangeType.CODE_CHANGED: 0.85,
    ChangeType.ENV_CHANGED: 0.5,
    ChangeType.ADDED: 0.7,
    ChangeType.REMOVED: 0.7,
}


def _score_diffs(
    diffs: list[AtomicDiff],
    graph_a: LineageGraph,
    graph_b: LineageGraph,
) -> list[AtomicDiff]:
    """Score each diff by impact and identify the root cause.

    Impact = severity_weight * proximity_factor

    Proximity is based on the step number relative to the total pipeline depth.
    Earlier steps (closer to raw inputs) that differ are more likely to be
    root causes; later diffs are more likely to be consequences.
    """
    if not diffs:
        return diffs

    # Build step-number index for proximity calculation
    all_steps_a = {j.step_number for j in graph_a.jobs if j.step_number}
    all_steps_b = {j.step_number for j in graph_b.jobs if j.step_number}
    max_step = max(max(all_steps_a, default=1), max(all_steps_b, default=1))

    scored: list[AtomicDiff] = []
    for d in diffs:
        severity = _SEVERITY_WEIGHTS.get(d.change_type, 0.5)

        # Extract step number from detail
        step_str = d.detail.get("step", "")
        step_num = _parse_step_number(step_str)

        # Proximity: earlier steps get higher scores (they're more likely root causes)
        if step_num is not None and max_step > 0:
            # Invert: step 1 of 8 -> proximity 1.0, step 8 of 8 -> proximity ~0.3
            proximity = 1.0 - (0.7 * (step_num - 1) / max_step)
        else:
            proximity = 0.5

        impact = round(severity * proximity, 3)
        scored.append(AtomicDiff(
            change_type=d.change_type,
            category=d.category,
            description=d.description,
            detail=d.detail,
            impact=impact,
            is_root_cause=False,
        ))

    # Sort by impact descending
    scored.sort(key=lambda d: -d.impact)

    # Mark root cause: the highest-impact diff at the earliest step
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


def _parse_step_number(step_str: str) -> int | None:
    """Extract step number from '@N' string."""
    if step_str.startswith("@"):
        try:
            return int(step_str[1:])
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Phase 5: LLM-assisted analysis
# ---------------------------------------------------------------------------


def _generate_llm_analysis(result: DiffResult) -> str | None:
    """Send the structured diff to Claude for natural-language interpretation."""
    try:
        import anthropic
    except ImportError:
        return "(--analyze requires the 'anthropic' package: pip install anthropic)"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(--analyze requires ANTHROPIC_API_KEY environment variable)"

    # Build a compact JSON representation for the prompt
    diff_data = {
        "ref_a": result.ref_a,
        "ref_b": result.ref_b,
        "target_a_hash": result.target_a_hash,
        "target_b_hash": result.target_b_hash,
        "targets_identical": result.targets_identical,
        "diffs": [
            {
                "change_type": d.change_type.value,
                "category": d.category.value,
                "description": d.description,
                "impact": d.impact,
                "is_root_cause": d.is_root_cause,
            }
            for d in result.diffs
        ],
        "matched_steps": len(result.matched_jobs),
        "steps_only_in_a": len(result.only_in_a),
        "steps_only_in_b": len(result.only_in_b),
    }
    if result.root_cause:
        diff_data["root_cause"] = {
            "description": result.root_cause.description,
            "category": result.root_cause.category.value,
            "impact": result.root_cause.impact,
        }

    prompt = f"""Analyze this ML pipeline provenance diff and explain what changed and why it matters.

The diff compares two artifacts produced by ML pipelines tracked by 'roar', a provenance tool.
Each diff has a category (data, params, code, compute, pipeline), a change_type, and an
impact score (0-1, higher = more likely to be the root cause).

Structured diff:
{json.dumps(diff_data, indent=2)}

Provide a concise (3-5 sentence) analysis:
1. What is the primary reason these artifacts differ?
2. Which changes are root causes vs. consequences?
3. Any concerns (e.g., non-determinism, environment drift)?

Be direct and specific. Reference step numbers and file names."""

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        return f"(LLM analysis failed: {e})"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_graph(
    db_ctx: QueryDatabaseContext | None,
    ref: str,
    cwd: Path,
    max_depth: int,
) -> LineageGraph:
    """Resolve a reference to a LineageGraph, dispatching by ref type."""
    ref_type = _classify_ref(ref)

    if ref_type == "glaas":
        return _resolve_glaas_ref(ref)

    if ref_type == "session":
        if db_ctx is None:
            raise DiffError("Local database required for session refs.")
        session = _resolve_session_ref(db_ctx, ref)
        return _extract_session_lineage(db_ctx, session)

    # All other types need local DB
    if db_ctx is None:
        raise DiffError("Local database required for local refs.")
    art_id, path = _resolve_ref_to_artifact_id(db_ctx, ref, cwd)
    return _extract_lineage(db_ctx, art_id, path, max_depth)


def build_diff(request: DiffQueryRequest) -> DiffResult:
    """Build a diff between two references."""
    max_depth = request.depth if request.depth is not None else 10

    ref_a_type = _classify_ref(request.ref_a)
    ref_b_type = _classify_ref(request.ref_b)
    needs_local = ref_a_type != "glaas" or ref_b_type != "glaas"

    if needs_local:
        with create_query_database_context(request.roar_dir) as db_ctx:
            graph_a = _resolve_graph(db_ctx, request.ref_a, request.cwd, max_depth)
            graph_b = _resolve_graph(db_ctx, request.ref_b, request.cwd, max_depth)
    else:
        # Both refs are remote — no local DB needed
        graph_a = _resolve_graph(None, request.ref_a, request.cwd, max_depth)
        graph_b = _resolve_graph(None, request.ref_b, request.cwd, max_depth)

    # Match jobs
    matched, only_a, only_b = _match_jobs(graph_a, graph_b)

    # Compute diffs
    diffs = _diff_matched_jobs(matched)

    # Add diffs for unmatched jobs (structural changes)
    for j in only_a:
        step = f"@{j.step_number}" if j.step_number else j.job_uid
        diffs.append(AtomicDiff(
            change_type=ChangeType.REMOVED,
            category=DiffCategory.PIPELINE,
            description=f"Step {step} only in A: {j.command}",
            detail={"step": step, "command": j.command},
        ))
    for j in only_b:
        step = f"@{j.step_number}" if j.step_number else j.job_uid
        diffs.append(AtomicDiff(
            change_type=ChangeType.ADDED,
            category=DiffCategory.PIPELINE,
            description=f"Step {step} only in B: {j.command}",
            detail={"step": step, "command": j.command},
        ))

    # Phase 4: Score and rank diffs
    diffs = _score_diffs(diffs, graph_a, graph_b)

    targets_identical = (
        graph_a.target_hash is not None
        and graph_b.target_hash is not None
        and graph_a.target_hash == graph_b.target_hash
    )

    root_cause = next((d for d in diffs if d.is_root_cause), None)

    result = DiffResult(
        ref_a=request.ref_a,
        ref_b=request.ref_b,
        target_a_hash=graph_a.target_hash,
        target_b_hash=graph_b.target_hash,
        target_a_path=graph_a.target_path,
        target_b_path=graph_b.target_path,
        targets_identical=targets_identical,
        diffs=diffs,
        matched_jobs=matched,
        only_in_a=only_a,
        only_in_b=only_b,
        root_cause=root_cause,
    )

    # Phase 5: LLM analysis (optional)
    if request.analyze and not targets_identical:
        result.analysis = _generate_llm_analysis(result)

    return result


def render_diff(request: DiffQueryRequest) -> str:
    """Build and render a diff between two references."""
    from ...presenters.diff_renderer import DiffRenderer

    result = build_diff(request)
    renderer = DiffRenderer()

    if request.output_json:
        return renderer.render_json(result)
    return renderer.render_text(result, format=request.format)
