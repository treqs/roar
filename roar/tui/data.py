"""Data adapter: thin wrappers around roar.application.query for the TUI.

The TUI is a pure view layer — every read routes through the same functions
the CLI uses (`render_dag`, `build_show_summary`, `build_log_summary`) so there
is no parallel data path to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..application.query.dag import build_dag_visualization
from ..application.query.log import build_log_summary
from ..application.query.requests import (
    DagQueryRequest,
    LogQueryRequest,
    ShowQueryRequest,
)
from ..application.query.results import (
    LogSummary,
    ShowArtifactSummary,
    ShowJobSummary,
    ShowSessionSummary,
)
from ..application.query.show import ShowQueryError, build_show_summary
from ..core.models.dag import DagVisualization
from ..db.query_context import create_query_database_context


def load_dag(
    roar_dir: Path,
    *,
    expanded: bool = False,
    show_artifacts: bool = True,
    stale_only: bool = False,
    session_ref: str | None = None,
) -> DagVisualization:
    """Fetch the structured DAG for a session (active by default)."""
    return build_dag_visualization(
        DagQueryRequest(
            roar_dir=roar_dir,
            expanded=expanded,
            output_json=False,
            use_color=False,
            show_artifacts=show_artifacts,
            stale_only=stale_only,
            session_ref=session_ref,
        )
    )


@dataclass(frozen=True)
class SessionListing:
    """Lightweight session row for the picker (avoids per-row heavyweight summaries)."""

    hash: str
    short_hash: str
    created_at: float | None
    is_active: bool
    job_count: int


def list_sessions(roar_dir: Path) -> list[SessionListing]:
    """All sessions in this project, newest first, marking which is active."""
    with create_query_database_context(roar_dir) as db_ctx:
        active = db_ctx.sessions.get_active()
        active_id = int(active["id"]) if active else None
        rows: list[SessionListing] = []
        for session in db_ctx.sessions.get_all():
            sid = int(session["id"])
            session_hash = session.get("hash") or ""
            jobs = db_ctx.jobs.get_by_session(sid)
            rows.append(
                SessionListing(
                    hash=session_hash,
                    short_hash=session_hash[:12] if session_hash else "-",
                    created_at=session.get("created_at"),
                    is_active=(sid == active_id),
                    job_count=len(jobs),
                )
            )
    return rows


def load_session(
    roar_dir: Path, session_ref: str | None = None
) -> ShowSessionSummary | None:
    """Fetch a session summary by ref (full hash / prefix), or active when None."""
    try:
        summary = build_show_summary(
            ShowQueryRequest(
                roar_dir=roar_dir,
                cwd=roar_dir.parent,
                ref=None,
                selector="session",
                session_ref=session_ref,
            )
        )
    except ShowQueryError:
        return None
    if isinstance(summary, ShowSessionSummary):
        return summary
    return None


def load_active_session(roar_dir: Path) -> ShowSessionSummary | None:
    """Back-compat shim — prefer `load_session(roar_dir)` directly."""
    return load_session(roar_dir)


def load_job(
    roar_dir: Path, cwd: Path, ref: str, session_ref: str | None = None
) -> ShowJobSummary | None:
    """Fetch a job summary by step ref (@N/@BN) or job UID.

    `session_ref` scopes `@N` resolution to a specific session; ignored for
    job-uid refs (those are unique across sessions).
    """
    try:
        summary = build_show_summary(
            ShowQueryRequest(
                roar_dir=roar_dir,
                cwd=cwd,
                ref=ref,
                selector="job",
                session_ref=session_ref,
            )
        )
    except ShowQueryError:
        return None
    if isinstance(summary, ShowJobSummary):
        return summary
    return None


def load_artifact_by_path(roar_dir: Path, cwd: Path, path: str) -> ShowArtifactSummary | None:
    try:
        summary = build_show_summary(
            ShowQueryRequest(roar_dir=roar_dir, cwd=cwd, ref=path, selector="path")
        )
    except ShowQueryError:
        return None
    if isinstance(summary, ShowArtifactSummary):
        return summary
    return None


def load_artifact_by_hash(
    roar_dir: Path, cwd: Path, artifact_hash: str
) -> ShowArtifactSummary | None:
    try:
        summary = build_show_summary(
            ShowQueryRequest(roar_dir=roar_dir, cwd=cwd, ref=artifact_hash, selector="artifact")
        )
    except ShowQueryError:
        return None
    if isinstance(summary, ShowArtifactSummary):
        return summary
    return None


def load_log(roar_dir: Path) -> LogSummary:
    """Fetch the active session's job history."""
    return build_log_summary(LogQueryRequest(roar_dir=roar_dir, use_color=False))


def load_command_history(roar_dir: Path, limit: int = 500) -> list[str]:
    """Distinct recent `command` strings across **all** project sessions, newest first.

    The launcher's history search is most useful when it covers everything the
    user has ever run — typing `train` at the launcher should turn up training
    commands from any historical session, not just the active one.
    """
    commands: list[str] = []
    seen: set[str] = set()
    with create_query_database_context(roar_dir) as db_ctx:
        for session in db_ctx.sessions.get_all():
            for job in db_ctx.jobs.get_by_session(int(session["id"]), limit=limit):
                cmd = job.get("command")
                if cmd and cmd not in seen:
                    seen.add(cmd)
                    commands.append(cmd)
                    if len(commands) >= limit:
                        return commands
    return commands


SearchKind = Literal["job", "artifact"]


@dataclass(frozen=True)
class SearchHit:
    kind: SearchKind
    label: str  # human display
    target_ref: str  # what to feed back to the detail view: "@N", path, or hash


def search(roar_dir: Path, query: str, limit: int = 50) -> list[SearchHit]:
    """Substring search over jobs (by command) and artifacts (by path) in the active session.

    Simple and cheap — we scan the active session only for v1. Broadening to all
    sessions is a v2 task once the session browser lands.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    hits: list[SearchHit] = []
    with create_query_database_context(roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if session is None:
            return []
        session_id = int(session["id"])

        for job in db_ctx.jobs.get_by_session(session_id, limit=500):
            command = (job.get("command") or "").lower()
            uid = (job.get("job_uid") or "").lower()
            if needle in command or needle in uid:
                step_number = job.get("step_number")
                job_type = job.get("job_type")
                if step_number is not None:
                    prefix = "@B" if job_type == "build" else "@"
                    ref = f"{prefix}{step_number}"
                else:
                    ref = str(job.get("job_uid") or "")
                display_cmd = job.get("command") or "(no command)"
                hits.append(SearchHit(kind="job", label=f"{ref}  {display_cmd}", target_ref=ref))
                if len(hits) >= limit:
                    return hits

        # Artifacts: iterate from the DAG view (already filtered by session).
        dag = build_dag_visualization(
            DagQueryRequest(
                roar_dir=roar_dir,
                expanded=False,
                output_json=False,
                use_color=False,
                show_artifacts=True,
                stale_only=False,
            )
        )
        for artifact in dag.artifacts:
            path = (artifact.path or "").lower()
            ahash = (artifact.hash or "").lower()
            if needle in path or needle in ahash:
                display = artifact.path or artifact.hash or "(unknown)"
                ref = artifact.path or artifact.hash or ""
                hits.append(SearchHit(kind="artifact", label=display, target_ref=ref))
                if len(hits) >= limit:
                    break

    return hits
