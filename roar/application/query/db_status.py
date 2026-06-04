"""Local database status query — footprint, contents, sync state, hygiene, age.

This is a view over the SQLite database *itself* (its size, row counts, how much
is synced to GLaaS, and gc-relevant hygiene), distinct from ``roar status`` which
summarizes the active session / workflow. ``gc`` will build on the hygiene + age
signals surfaced here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...db.context import _get_sqlite_module
from ...presenters.formatting import format_size
from .requests import DbStatusQueryRequest

_DAY = 86_400
# Age buckets over artifact first_seen_at — the entity gc will target. Counts,
# not a sort, so they stay cheap and answer "what would an N-day prune reclaim".
_AGE_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("<=7d", 0.0, 7.0),
    ("7-30d", 7.0, 30.0),
    ("30-90d", 30.0, 90.0),
    (">90d", 90.0, None),
)


class DbStatusQueryError(RuntimeError):
    """Raised when the local database can't be inspected."""


@dataclass(frozen=True)
class EntitySync:
    """Synced-to-GLaaS coverage for one entity type."""

    total: int
    synced: int

    @property
    def unsynced(self) -> int:
        return max(self.total - self.synced, 0)

    @property
    def pct(self) -> int | None:
        if self.total == 0:
            return None
        return round(100 * self.synced / self.total)


@dataclass(frozen=True)
class DbStatusSummary:
    db_path: Path
    exists: bool
    size_bytes: int = 0
    schema_version: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    sync: dict[str, EntitySync] = field(default_factory=dict)
    orphan_artifacts: int = 0
    superseded_jobs: int = 0
    oldest: float | None = None
    newest: float | None = None
    age_buckets: dict[str, int] = field(default_factory=dict)


def _scalar(conn: Any, sql: str, params: tuple[Any, ...] = (), default: int = 0) -> int:
    """Run a scalar aggregate, tolerating a missing table/column (returns default)."""
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:  # sqlite OperationalError for absent table/column on older DBs
        return default
    if row is None or row[0] is None:
        return int(default)
    return int(row[0])


def _scalar_float(conn: Any, sql: str) -> float | None:
    try:
        row = conn.execute(sql).fetchone()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _entity_sync(conn: Any, table: str) -> EntitySync:
    total = _scalar(conn, f"SELECT COUNT(*) FROM {table}")
    synced = _scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE synced_at IS NOT NULL")
    return EntitySync(total=total, synced=synced)


def collect_db_status(db_path: Path, *, now: float | None = None) -> DbStatusSummary:
    """Gather DB status straight from the SQLite file (read-only, missing-safe)."""
    if not db_path.exists():
        return DbStatusSummary(db_path=db_path, exists=False)

    now = time.time() if now is None else now
    sqlite_module = _get_sqlite_module()
    try:
        conn = sqlite_module.connect(str(db_path))
    except Exception as exc:  # pragma: no cover - defensive
        raise DbStatusQueryError(f"Failed to open database at {db_path}: {exc}") from exc

    try:
        size_bytes = db_path.stat().st_size

        schema_version: int | None = None
        try:
            schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except Exception:
            schema_version = None

        artifacts_total = _scalar(conn, "SELECT COUNT(*) FROM artifacts")
        artifacts_composite = _scalar(
            conn, "SELECT COUNT(*) FROM artifacts WHERE kind = 'composite'"
        )
        counts = {
            "sessions": _scalar(conn, "SELECT COUNT(*) FROM sessions"),
            "jobs": _scalar(conn, "SELECT COUNT(*) FROM jobs"),
            "artifacts": artifacts_total,
            "artifacts_composite": artifacts_composite,
            "artifacts_primitive": artifacts_total - artifacts_composite,
            "components": _scalar(conn, "SELECT COUNT(*) FROM composite_artifact_components"),
            "labels": _scalar(conn, "SELECT COUNT(*) FROM labels"),
            "links_in": _scalar(conn, "SELECT COUNT(*) FROM job_inputs"),
            "links_out": _scalar(conn, "SELECT COUNT(*) FROM job_outputs"),
            "collections": _scalar(conn, "SELECT COUNT(*) FROM collections"),
        }
        counts["links"] = counts["links_in"] + counts["links_out"]

        sync = {
            "sessions": _entity_sync(conn, "sessions"),
            "jobs": _entity_sync(conn, "jobs"),
            "artifacts": _entity_sync(conn, "artifacts"),
            "labels": _entity_sync(conn, "labels"),
        }

        orphan_artifacts = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM artifacts a
            WHERE NOT EXISTS (SELECT 1 FROM job_inputs ji WHERE ji.artifact_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM job_outputs jo WHERE jo.artifact_id = a.id)
            """,
        )
        # Re-run executions: jobs sharing a step_identity beyond the first.
        jobs_with_identity = _scalar(
            conn, "SELECT COUNT(*) FROM jobs WHERE step_identity IS NOT NULL"
        )
        distinct_identities = _scalar(
            conn, "SELECT COUNT(DISTINCT step_identity) FROM jobs WHERE step_identity IS NOT NULL"
        )
        superseded_jobs = max(jobs_with_identity - distinct_identities, 0)

        # Span across the timestamped tables → the DB's real lifespan.
        oldest = _min_across(
            conn,
            [
                "SELECT MIN(first_seen_at) FROM artifacts",
                "SELECT MIN(timestamp) FROM jobs",
                "SELECT MIN(created_at) FROM sessions",
            ],
        )
        newest = _max_across(
            conn,
            [
                "SELECT MAX(first_seen_at) FROM artifacts",
                "SELECT MAX(timestamp) FROM jobs",
                "SELECT MAX(created_at) FROM sessions",
            ],
        )

        age_buckets: dict[str, int] = {}
        for label, lo_days, hi_days in _AGE_BUCKETS:
            clauses = ["first_seen_at IS NOT NULL"]
            params: list[Any] = []
            if hi_days is not None:  # younger bound (older than lo, no older than hi)
                clauses.append("first_seen_at > ?")
                params.append(now - hi_days * _DAY)
            if lo_days:  # older bound
                clauses.append("first_seen_at <= ?")
                params.append(now - lo_days * _DAY)
            where = " AND ".join(clauses)
            age_buckets[label] = _scalar(
                conn, f"SELECT COUNT(*) FROM artifacts WHERE {where}", tuple(params)
            )

        return DbStatusSummary(
            db_path=db_path,
            exists=True,
            size_bytes=size_bytes,
            schema_version=schema_version,
            counts=counts,
            sync=sync,
            orphan_artifacts=orphan_artifacts,
            superseded_jobs=superseded_jobs,
            oldest=oldest,
            newest=newest,
            age_buckets=age_buckets,
        )
    finally:
        conn.close()


def _min_across(conn: Any, queries: list[str]) -> float | None:
    values = [v for v in (_scalar_float(conn, q) for q in queries) if v is not None]
    return min(values) if values else None


def _max_across(conn: Any, queries: list[str]) -> float | None:
    values = [v for v in (_scalar_float(conn, q) for q in queries) if v is not None]
    return max(values) if values else None


def render_db_status(request: DbStatusQueryRequest) -> str:
    summary = collect_db_status(request.roar_dir / "roar.db")
    return _render(summary)


def _render(summary: DbStatusSummary) -> str:
    if not summary.exists:
        return f"No local database at {summary.db_path}.\nRun `roar init` to create one."

    c = summary.counts
    lines: list[str] = []

    lines.append("Database:")
    lines.append(f"  Path:    {summary.db_path}")
    lines.append(f"  Size:    {format_size(summary.size_bytes)}")
    schema = f"v{summary.schema_version}" if summary.schema_version is not None else "unknown"
    lines.append(f"  Schema:  {schema}")
    lines.append(f"  Span:    {_format_span(summary.oldest, summary.newest)}")

    lines.append("")
    lines.append("Contents:")
    lines.append(f"  Sessions:    {c.get('sessions', 0)}")
    lines.append(f"  Jobs:        {c.get('jobs', 0)}")
    lines.append(
        f"  Artifacts:   {c.get('artifacts', 0)}  "
        f"({c.get('artifacts_primitive', 0)} primitive, "
        f"{c.get('artifacts_composite', 0)} composite)"
    )
    lines.append(f"  Components:  {c.get('components', 0)}")
    lines.append(f"  Labels:      {c.get('labels', 0)}")
    lines.append(
        f"  Links:       {c.get('links', 0)}  "
        f"(in {c.get('links_in', 0)} / out {c.get('links_out', 0)})"
    )
    lines.append(f"  Collections: {c.get('collections', 0)}")

    lines.append("")
    lines.append("Synced to GLaaS:")
    for label, key in (
        ("Sessions", "sessions"),
        ("Jobs", "jobs"),
        ("Artifacts", "artifacts"),
        ("Labels", "labels"),
    ):
        es = summary.sync.get(key, EntitySync(0, 0))
        pct = "—" if es.pct is None else f"{es.pct}%"
        lines.append(f"  {label + ':':<11} {es.synced}/{es.total}  ({pct})")

    lines.append("")
    lines.append("Hygiene:")
    lines.append(f"  Orphan artifacts:        {summary.orphan_artifacts}")
    lines.append(f"  Superseded executions:   {summary.superseded_jobs}")

    if c.get("artifacts", 0):
        lines.append("")
        lines.append("Artifact age:")
        total = c.get("artifacts", 0)
        for label, _lo, _hi in _AGE_BUCKETS:
            n = summary.age_buckets.get(label, 0)
            bucket_pct = round(100 * n / total) if total else 0
            lines.append(f"  {label + ':':<8} {n:>5}  ({bucket_pct}%)")

    return "\n".join(lines)


def _format_span(oldest: float | None, newest: float | None) -> str:
    if oldest is None or newest is None:
        return "(no records)"
    from ...presenters.formatting import format_timestamp

    days = max(int((newest - oldest) / _DAY), 0)
    return (
        f"{format_timestamp(oldest)[:10]} → {format_timestamp(newest)[:10]} "
        f"({days} day{'s' if days != 1 else ''})"
    )
