"""Application orchestration for generating TReqs workflow YAML from local sessions."""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from ...core.step_name import resolve_step_name
from ...db.context import create_database_context
from ...db.step_priority import step_sort_key
from ...publish_auth import load_publish_auth_context, resolve_publish_creator_identity
from ..publish.collection import resolve_local_session_target
from ..publish.lineage import LineageCollector
from ..publish.session import compute_canonical_lineage_session_hash
from .requests import GenerateWorkflowRequest
from .results import GeneratedWorkflowTask, GenerateWorkflowResult

_NO_ACTIVE_SESSION_MESSAGE = "No active session. Run 'roar run' to create a session first."
_RESERVED_WORKFLOW_KEYS = frozenset({"name", "working_directory", "secrets"})
_FILENAME_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class _LiteralBlockString(str):
    """Marker for YAML literal-block string rendering."""


def _represent_literal_block_string(
    dumper: yaml.SafeDumper,
    data: _LiteralBlockString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.SafeDumper.add_representer(_LiteralBlockString, _represent_literal_block_string)


class WorkflowGenerationError(RuntimeError):
    """Raised when a TReqs workflow cannot be generated from local lineage."""


@dataclass(frozen=True)
class _PreparedTask:
    step_ref: str
    task_name: str
    command: str
    cwd: str | None
    env_vars: dict[str, str]


def generate_workflow(request: GenerateWorkflowRequest) -> GenerateWorkflowResult:
    """Generate a TReqs workflow YAML file from a local roar session."""
    lineage_collector = LineageCollector()

    with create_database_context(request.roar_dir) as db_ctx:
        session, session_hash = _resolve_session(
            db_ctx=db_ctx,
            request=request,
            lineage_collector=lineage_collector,
        )
        session_id = int(session["id"])
        steps = _representative_steps(db_ctx.sessions.get_steps(session_id))
        if not steps:
            raise WorkflowGenerationError(
                "Selected session has no tracked steps. Run 'roar run' or 'roar build' first."
            )

        tasks = _prepare_tasks(db_ctx, steps)
        workflow_name = request.workflow_name or _default_workflow_name(
            repo_root=request.roar_dir.parent,
            session_hash=session_hash,
        )
        working_directory = _shared_working_directory(tasks)
        payload = _build_workflow_payload(
            workflow_name=workflow_name,
            working_directory=working_directory,
            tasks=tasks,
        )
        rendered = _render_workflow_yaml(
            payload=payload,
            session_hash=session_hash,
            session_id=session_id,
        )

    output_path = _resolve_output_path(request, workflow_name)
    if output_path.exists() and not request.force:
        raise WorkflowGenerationError(
            f"Workflow output already exists: {output_path}. Use --force to overwrite it."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    return GenerateWorkflowResult(
        output_path=output_path,
        display_path=_display_path(output_path, request.roar_dir.parent),
        workflow_name=workflow_name,
        session_hash=session_hash,
        session_id=session_id,
        working_directory=working_directory,
        tasks=tuple(
            GeneratedWorkflowTask(step_ref=task.step_ref, task_name=task.task_name)
            for task in tasks
        ),
    )


def _resolve_session(
    *,
    db_ctx: Any,
    request: GenerateWorkflowRequest,
    lineage_collector: LineageCollector,
) -> tuple[dict[str, Any], str]:
    session_ref = str(request.session_ref or "current").strip() or "current"
    if session_ref == "current":
        session = db_ctx.sessions.get_active()
        if not session:
            raise WorkflowGenerationError(_NO_ACTIVE_SESSION_MESSAGE)
        return session, _canonical_session_hash(
            session_id=int(session["id"]),
            roar_dir=request.roar_dir,
            lineage_collector=lineage_collector,
        )

    session, resolved_hash, error = resolve_local_session_target(
        db_ctx=db_ctx,
        roar_dir=request.roar_dir,
        session_hash=session_ref,
        session_service=db_ctx.session_service,
        lineage_collector=lineage_collector,
    )
    if error is not None:
        raise WorkflowGenerationError(error)
    if session is None or resolved_hash is None:
        raise WorkflowGenerationError(f"No local session matches '{session_ref}'.")
    return session, resolved_hash


def _canonical_session_hash(
    *,
    session_id: int,
    roar_dir: Path,
    lineage_collector: LineageCollector,
) -> str:
    lineage = lineage_collector.collect_session(session_id, roar_dir)
    if not getattr(lineage, "jobs", None):
        raise WorkflowGenerationError(
            "Selected session has no tracked steps. Run 'roar run' or 'roar build' first."
        )
    creator_identity = resolve_publish_creator_identity(
        load_publish_auth_context(roar_dir.parent, allow_public_without_binding=True)
    )
    return compute_canonical_lineage_session_hash(
        lineage=lineage,
        creator_identity=creator_identity,
    )


def _representative_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_number: dict[int, list[dict[str, Any]]] = {}
    for step in steps:
        step_number = step.get("step_number")
        if not isinstance(step_number, int):
            continue
        by_number.setdefault(step_number, []).append(step)

    return [max(by_number[number], key=step_sort_key) for number in sorted(by_number.keys())]


def _prepare_tasks(db_ctx: Any, steps: list[dict[str, Any]]) -> list[_PreparedTask]:
    prepared: list[_PreparedTask] = []
    used_names: set[str] = set()

    for step in steps:
        labels = _current_job_labels(db_ctx, int(step["id"]))
        raw_task_name = _preferred_task_name(step, labels)
        task_name = _dedupe_task_name(raw_task_name, used_names)
        metadata = _parse_metadata(step.get("metadata"))
        prepared.append(
            _PreparedTask(
                step_ref=_step_ref(step),
                task_name=task_name,
                command=str(step.get("command") or "").strip(),
                cwd=_normalize_cwd(metadata.get("cwd")),
                env_vars=_normalize_env_vars(metadata.get("env_vars")),
            )
        )

    return prepared


def _current_job_labels(db_ctx: Any, job_id: int) -> dict[str, Any]:
    current = db_ctx.labels.get_current("job", job_id=job_id)
    metadata = current.get("metadata") if isinstance(current, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def _preferred_task_name(step: dict[str, Any], labels: dict[str, Any]) -> str:
    resolved_name = resolve_step_name(labels, step.get("step_name"))
    if isinstance(resolved_name, str) and resolved_name.strip():
        return resolved_name.strip()

    script = str(step.get("script") or "").strip()
    if script:
        stem = Path(script).name
        if stem.endswith((".py", ".sh")):
            stem = Path(stem).stem
        stem = stem.replace(".", "-")
        if stem:
            return stem

    step_number = step.get("step_number")
    if step.get("job_type") == "build":
        return f"build-{step_number}"
    return f"step-{step_number}"


def _dedupe_task_name(candidate: str, used_names: set[str]) -> str:
    base = candidate.strip() or "task"
    if base in _RESERVED_WORKFLOW_KEYS:
        base = f"{base}-task"
    name = base
    suffix = 2
    while name in used_names or name in _RESERVED_WORKFLOW_KEYS:
        name = f"{base}-{suffix}"
        suffix += 1
    used_names.add(name)
    return name


def _parse_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if not isinstance(raw_metadata, str) or not raw_metadata.strip():
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_cwd(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized in {"", "."}:
        return None
    return normalized


def _normalize_env_vars(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for key in sorted(value.keys()):
        if not isinstance(key, str) or not key:
            continue
        normalized[key] = _stringify_env_value(value[key])
    return normalized


def _stringify_env_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _step_ref(step: dict[str, Any]) -> str:
    step_number = int(step.get("step_number") or 0)
    prefix = "@B" if step.get("job_type") == "build" else "@"
    return f"{prefix}{step_number}"


def _shared_working_directory(tasks: list[_PreparedTask]) -> str | None:
    cwd_values = {task.cwd or "" for task in tasks}
    if len(cwd_values) != 1:
        return None
    only_value = next(iter(cwd_values))
    return only_value or None


def _build_workflow_payload(
    *,
    workflow_name: str,
    working_directory: str | None,
    tasks: list[_PreparedTask],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": workflow_name}
    if working_directory:
        payload["working_directory"] = working_directory

    for task in tasks:
        payload[task.task_name] = _LiteralBlockString(
            _render_task_command(task, global_working_directory=working_directory)
        )
    return payload


def _render_task_command(task: _PreparedTask, *, global_working_directory: str | None) -> str:
    lines: list[str] = []
    for key, value in task.env_vars.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    if task.cwd and task.cwd != global_working_directory:
        lines.append(f"cd {shlex.quote(task.cwd)}")
    lines.append(task.command)
    return "\n".join(lines)


def _render_workflow_yaml(*, payload: dict[str, Any], session_hash: str, session_id: int) -> str:
    header = (
        "# Generated by `roar workflow generate`\n"
        f"# Source DAG hash: {session_hash}\n"
        f"# Source session id: {session_id}\n"
        f"# Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
    )
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return header + body


def _default_workflow_name(*, repo_root: Path, session_hash: str) -> str:
    repo_name = repo_root.name or "roar-session"
    return f"{repo_name}-{session_hash[:12]}"


def _resolve_output_path(request: GenerateWorkflowRequest, workflow_name: str) -> Path:
    if request.output_path is not None:
        output_path = request.output_path
        if not output_path.is_absolute():
            output_path = request.cwd / output_path
        return output_path.resolve()

    filename = f"{_filename_slug(workflow_name)}.yaml"
    return (request.roar_dir.parent / ".treqs" / "workflows" / filename).resolve()


def _filename_slug(value: str) -> str:
    slug = _FILENAME_SLUG_RE.sub("-", value.strip().lower()).strip("-._")
    return slug or "workflow"


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return os.fspath(path)
