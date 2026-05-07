"""Lightweight sqlite-backed context for hot read-only query commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.exceptions import DatabaseConnectionError
from .context import _get_sqlite_module
from .step_priority import step_sort_key


class QueryDatabaseContext:
    """Context manager providing lightweight read-only query access."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._sqlite_module = _get_sqlite_module()
        self._connection: Any | None = None
        self._sessions_repo = _QuerySessionRepository(self)
        self._jobs_repo = _QueryJobRepository(self)
        self._artifacts_repo = _QueryArtifactRepository(self)
        self._labels_repo = _QueryLabelRepository(self)
        self._composites_repo = _QueryCompositeRepository(self)

    def connect(self) -> None:
        """Open a direct sqlite connection when the database exists."""
        if not self.db_path.exists():
            return

        try:
            self._connection = self._sqlite_module.connect(str(self.db_path))
            self._connection.row_factory = self._sqlite_module.Row
        except Exception as exc:  # pragma: no cover - defensive parity with DB context
            raise DatabaseConnectionError(f"Failed to connect to database: {exc}") from exc

    def close(self) -> None:
        """Close the sqlite connection if one was opened."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> QueryDatabaseContext:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def sessions(self) -> _QuerySessionRepository:
        return self._sessions_repo

    @property
    def jobs(self) -> _QueryJobRepository:
        return self._jobs_repo

    @property
    def artifacts(self) -> _QueryArtifactRepository:
        return self._artifacts_repo

    @property
    def labels(self) -> _QueryLabelRepository:
        return self._labels_repo

    @property
    def composites(self) -> _QueryCompositeRepository:
        return self._composites_repo

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        if self._connection is None:
            return []

        try:
            cursor = self._connection.execute(sql, params)
            return cursor.fetchall()
        except self._sqlite_module.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._fetchall(sql, params)
        if not rows:
            return None
        return rows[0]


class _QueryRepository:
    """Shared helpers for direct sqlite repositories."""

    def __init__(self, db_ctx: QueryDatabaseContext):
        self._db_ctx = db_ctx

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        return self._db_ctx._fetchall(sql, params)

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        return self._db_ctx._fetchone(sql, params)


class _QuerySessionRepository(_QueryRepository):
    def get(self, session_id: int) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT id, hash, created_at, source_artifact_hash, current_step, is_active,
                   git_repo, git_commit_start, git_commit_end, synced_at, metadata
            FROM sessions
            WHERE id = ?
            LIMIT 1
            """,
            (session_id,),
        )
        return _session_row_to_dict(row) if row is not None else None

    def get_active(self) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT id, hash, created_at, source_artifact_hash, current_step, is_active,
                   git_repo, git_commit_start, git_commit_end, synced_at, metadata
            FROM sessions
            WHERE is_active = 1
            LIMIT 1
            """
        )
        return _session_row_to_dict(row) if row is not None else None

    def get_all(self) -> list[dict[str, Any]]:
        """All sessions, newest first."""
        rows = self._fetchall(
            """
            SELECT id, hash, created_at, source_artifact_hash, current_step, is_active,
                   git_repo, git_commit_start, git_commit_end, synced_at, metadata
            FROM sessions
            ORDER BY created_at DESC, id DESC
            """
        )
        return [_session_row_to_dict(row) for row in rows]

    def get_by_hash(self, session_hash: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT id, hash, created_at, source_artifact_hash, current_step, is_active,
                   git_repo, git_commit_start, git_commit_end, synced_at, metadata
            FROM sessions
            WHERE hash = ?
            LIMIT 1
            """,
            (session_hash,),
        )
        return _session_row_to_dict(row) if row is not None else None

    def get_by_hash_prefix(self, hash_prefix: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT id, hash, created_at, source_artifact_hash, current_step, is_active,
                   git_repo, git_commit_start, git_commit_end, synced_at, metadata
            FROM sessions
            WHERE hash LIKE ?
            LIMIT 1
            """,
            (f"{hash_prefix}%",),
        )
        return _session_row_to_dict(row) if row is not None else None

    def get_steps(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT *
            FROM jobs
            WHERE session_id = ?
            ORDER BY step_number ASC, timestamp ASC
            """,
            (session_id,),
        )
        return [_job_row_to_dict(row) for row in rows]

    def get_step_by_number(
        self, session_id: int, step_number: int, job_type: str | None = None
    ) -> dict[str, Any] | None:
        if job_type == "build":
            rows = self._fetchall(
                """
                SELECT *
                FROM jobs
                WHERE session_id = ? AND step_number = ? AND job_type = 'build'
                """,
                (session_id, step_number),
            )
        else:
            rows = self._fetchall(
                """
                SELECT *
                FROM jobs
                WHERE session_id = ? AND step_number = ?
                  AND (job_type IS NULL OR job_type != 'build')
                """,
                (session_id, step_number),
            )

        jobs = [_job_row_to_dict(row) for row in rows]
        if not jobs:
            return None
        return max(jobs, key=step_sort_key)


class _QueryJobRepository(_QueryRepository):
    def get_by_parent_uids(
        self, parent_job_uids: list[str], job_type: str | None = None
    ) -> list[dict[str, Any]]:
        if not parent_job_uids:
            return []

        placeholders = ", ".join("?" for _ in parent_job_uids)
        sql = f"""
            SELECT *
            FROM jobs
            WHERE parent_job_uid IN ({placeholders})
        """
        params: list[Any] = [*parent_job_uids]
        if job_type is not None:
            sql += " AND job_type = ?"
            params.append(job_type)
        sql += " ORDER BY timestamp ASC"

        rows = self._fetchall(sql, tuple(params))
        return [_job_row_to_dict(row) for row in rows]

    def get_by_session(self, session_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT *
            FROM jobs
            WHERE session_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (session_id, limit),
        )
        return [_job_row_to_dict(row) for row in rows]

    def get_latest_build_jobs(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT j.*
            FROM jobs AS j
            JOIN (
                SELECT step_number, MAX(id) AS max_id
                FROM jobs
                WHERE session_id = ? AND job_type = 'build'
                GROUP BY step_number
            ) AS latest_build_ids ON j.id = latest_build_ids.max_id
            ORDER BY j.step_number ASC
            """,
            (session_id,),
        )
        return [_job_row_to_dict(row) for row in rows]

    def get_by_uid(self, job_uid: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM jobs WHERE job_uid = ? LIMIT 1", (job_uid,))
        if row is not None:
            return _job_row_to_dict(row)

        if len(job_uid) < 4:
            return None

        rows = self._fetchall(
            "SELECT * FROM jobs WHERE job_uid LIKE ? LIMIT 2",
            (f"{job_uid}%",),
        )
        if len(rows) != 1:
            return None
        return _job_row_to_dict(rows[0])

    def get_inputs(self, job_id: int) -> list[dict[str, Any]]:
        return self._get_job_artifacts("job_inputs", job_id)

    def get_outputs(self, job_id: int) -> list[dict[str, Any]]:
        return self._get_job_artifacts("job_outputs", job_id)

    def get_distinct_outputs_by_session(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT j.id AS job_id,
                   j.timestamp AS job_timestamp,
                   jo.path,
                   jo.artifact_id,
                   a.size,
                   a.first_seen_path,
                   a.kind,
                   a.component_count,
                   ah.algorithm,
                   ah.digest
            FROM jobs AS j
            JOIN job_outputs AS jo ON jo.job_id = j.id
            JOIN artifacts AS a ON a.id = jo.artifact_id
            LEFT JOIN artifact_hashes AS ah ON ah.artifact_id = a.id
            WHERE j.session_id = ?
            ORDER BY j.timestamp DESC, j.id DESC, jo.path ASC, ah.algorithm ASC, ah.digest ASC
            """,
            (session_id,),
        )

        results: list[dict[str, Any]] = []
        seen_artifact_ids: set[str] = set()
        current_by_artifact: dict[str, dict[str, Any]] = {}

        for row in rows:
            artifact_id = str(row["artifact_id"])
            if artifact_id not in current_by_artifact and artifact_id not in seen_artifact_ids:
                artifact = {
                    "artifact_id": artifact_id,
                    "artifact_hash": None,
                    "size": int(row["size"] or 0),
                    "path": str(row["path"] or row["first_seen_path"] or ""),
                    "kind": row["kind"],
                    "component_count": row["component_count"],
                    "hashes": [],
                }
                current_by_artifact[artifact_id] = artifact
                results.append(artifact)
                seen_artifact_ids.add(artifact_id)

            artifact_entry = current_by_artifact.get(artifact_id)
            if artifact_entry is None:
                continue

            algorithm = row["algorithm"]
            digest = row["digest"]
            if algorithm and digest:
                artifact_entry["hashes"].append(
                    {
                        "algorithm": str(algorithm),
                        "digest": str(digest),
                    }
                )
                if artifact_entry["artifact_hash"] is None:
                    artifact_entry["artifact_hash"] = str(digest)

        return results

    def _get_job_artifacts(self, table_name: str, job_id: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            f"""
            SELECT io.path,
                   io.artifact_id,
                   io.byte_ranges,
                   a.size,
                   a.first_seen_path,
                   a.kind,
                   a.component_count,
                   ah.algorithm,
                   ah.digest
            FROM {table_name} AS io
            JOIN artifacts AS a ON a.id = io.artifact_id
            LEFT JOIN artifact_hashes AS ah ON ah.artifact_id = a.id
            WHERE io.job_id = ?
            ORDER BY io.path ASC, io.artifact_id ASC, ah.algorithm ASC, ah.digest ASC
            """,
            (job_id,),
        )

        results: list[dict[str, Any]] = []
        current_key: tuple[Any, ...] | None = None
        current: dict[str, Any] | None = None

        for row in rows:
            key = (
                row["path"],
                row["artifact_id"],
                row["byte_ranges"],
                row["size"],
                row["first_seen_path"],
                row["kind"],
                row["component_count"],
            )
            if key != current_key:
                current = {
                    "path": str(row["path"] or row["first_seen_path"] or ""),
                    "artifact_id": str(row["artifact_id"]),
                    "size": int(row["size"] or 0),
                    "kind": row["kind"],
                    "component_count": row["component_count"],
                    "hashes": [],
                    "artifact_hash": None,
                    "first_seen_path": row["first_seen_path"],
                    "byte_ranges": json.loads(row["byte_ranges"]) if row["byte_ranges"] else None,
                }
                results.append(current)
                current_key = key

            if current is None:
                continue

            algorithm = row["algorithm"]
            digest = row["digest"]
            if algorithm and digest:
                current["hashes"].append(
                    {
                        "algorithm": str(algorithm),
                        "digest": str(digest),
                    }
                )
                if current["artifact_hash"] is None:
                    current["artifact_hash"] = str(digest)

        return results


class _QueryArtifactRepository(_QueryRepository):
    def get(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT id, size, first_seen_at, first_seen_path, source_type, source_url,
                   uploaded_to, synced_at, kind, component_count, metadata
            FROM artifacts
            WHERE id = ?
            LIMIT 1
            """,
            (artifact_id,),
        )
        if row is None:
            return None

        artifact = _artifact_row_to_dict(row)
        artifact["hashes"] = self.get_hashes(artifact_id)
        return artifact

    def get_hashes(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT algorithm, digest
            FROM artifact_hashes
            WHERE artifact_id = ?
            ORDER BY algorithm ASC, digest ASC
            """,
            (artifact_id,),
        )
        return [
            {
                "algorithm": str(row["algorithm"]),
                "digest": str(row["digest"]),
            }
            for row in rows
        ]

    def get_by_hash(self, digest: str, algorithm: str | None = None) -> dict[str, Any] | None:
        normalized_digest = digest.lower()
        if algorithm is None:
            rows = self._fetchall(
                """
                SELECT DISTINCT artifact_id
                FROM artifact_hashes
                WHERE digest LIKE ?
                LIMIT 2
                """,
                (f"{normalized_digest}%",),
            )
        else:
            rows = self._fetchall(
                """
                SELECT DISTINCT artifact_id
                FROM artifact_hashes
                WHERE algorithm = ? AND digest LIKE ?
                LIMIT 2
                """,
                (algorithm, f"{normalized_digest}%"),
            )

        if len(rows) != 1:
            return None

        artifact = self.get(str(rows[0]["artifact_id"]))
        if artifact is None:
            return None
        hashes = artifact.get("hashes", [])
        artifact["hash"] = hashes[0]["digest"] if hashes else None
        return artifact

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        output = self._fetchone(
            """
            SELECT jo.artifact_id
            FROM job_outputs AS jo
            JOIN jobs AS j ON j.id = jo.job_id
            WHERE jo.path = ?
            ORDER BY j.timestamp DESC
            LIMIT 1
            """,
            (path,),
        )
        if output is not None:
            return self.get(str(output["artifact_id"]))

        input_row = self._fetchone(
            """
            SELECT ji.artifact_id
            FROM job_inputs AS ji
            JOIN jobs AS j ON j.id = ji.job_id
            WHERE ji.path = ?
            ORDER BY j.timestamp DESC
            LIMIT 1
            """,
            (path,),
        )
        if input_row is not None:
            return self.get(str(input_row["artifact_id"]))

        artifact = self._fetchone(
            """
            SELECT id, size, first_seen_at, first_seen_path, source_type, source_url,
                   uploaded_to, synced_at, kind, component_count, metadata
            FROM artifacts
            WHERE first_seen_path = ?
            LIMIT 1
            """,
            (path,),
        )
        if artifact is None:
            return None

        result = _artifact_row_to_dict(artifact)
        result["hashes"] = self.get_hashes(str(artifact["id"]))
        return result

    def get_locations(self, artifact_id: str) -> list[dict[str, str]]:
        paths = {
            str(row["path"])
            for row in self._fetchall(
                "SELECT DISTINCT path FROM job_outputs WHERE artifact_id = ?",
                (artifact_id,),
            )
            if row["path"]
        }
        paths.update(
            {
                str(row["path"])
                for row in self._fetchall(
                    "SELECT DISTINCT path FROM job_inputs WHERE artifact_id = ?",
                    (artifact_id,),
                )
                if row["path"]
            }
        )

        artifact = self._fetchone(
            "SELECT first_seen_path FROM artifacts WHERE id = ?", (artifact_id,)
        )
        if artifact is not None and artifact["first_seen_path"]:
            paths.add(str(artifact["first_seen_path"]))

        return [{"path": path} for path in sorted(paths)]

    def get_jobs(self, artifact_id: str) -> dict[str, list[dict[str, Any]]]:
        produced_by = [
            self._job_row_with_session(row)
            for row in self._fetchall(
                """
                SELECT j.*, s.hash AS session_hash
                FROM jobs AS j
                JOIN job_outputs AS jo ON j.id = jo.job_id
                LEFT JOIN sessions AS s ON s.id = j.session_id
                WHERE jo.artifact_id = ?
                ORDER BY j.timestamp DESC
                """,
                (artifact_id,),
            )
        ]
        consumed_by = [
            self._job_row_with_session(row)
            for row in self._fetchall(
                """
                SELECT j.*, s.hash AS session_hash
                FROM jobs AS j
                JOIN job_inputs AS ji ON j.id = ji.job_id
                LEFT JOIN sessions AS s ON s.id = j.session_id
                WHERE ji.artifact_id = ?
                ORDER BY j.timestamp DESC
                """,
                (artifact_id,),
            )
        ]
        return {
            "produced_by": produced_by,
            "consumed_by": consumed_by,
        }

    @staticmethod
    def _job_row_with_session(row: Any) -> dict[str, Any]:
        data = _job_row_to_dict(row)
        data["session_hash"] = row["session_hash"]
        return data


class _QueryLabelRepository(_QueryRepository):
    def get_current(
        self,
        entity_type: str,
        *,
        session_id: int | None = None,
        job_id: int | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any] | None:
        if session_id is not None:
            clauses = "session_id = ? AND job_id IS NULL AND artifact_id IS NULL"
            params: tuple[Any, ...] = (entity_type, session_id)
        elif job_id is not None:
            clauses = "session_id IS NULL AND job_id = ? AND artifact_id IS NULL"
            params = (entity_type, job_id)
        elif artifact_id is not None:
            clauses = "session_id IS NULL AND job_id IS NULL AND artifact_id = ?"
            params = (entity_type, artifact_id)
        else:
            return None

        row = self._fetchone(
            f"""
            SELECT id, entity_type, session_id, job_id, artifact_id, version, metadata,
                   write_origin, created_at, synced_at, synced_server_label_id
            FROM labels
            WHERE entity_type = ? AND {clauses}
            ORDER BY version DESC
            LIMIT 1
            """,
            params,
        )
        if row is None:
            return None

        return {
            "id": int(row["id"]),
            "entity_type": str(row["entity_type"]),
            "session_id": row["session_id"],
            "job_id": row["job_id"],
            "artifact_id": row["artifact_id"],
            "version": int(row["version"]),
            "metadata": json.loads(row["metadata"]),
            "write_origin": row["write_origin"],
            "created_at": row["created_at"],
            "synced_at": row["synced_at"],
            "synced_server_label_id": row["synced_server_label_id"],
        }


class _QueryCompositeRepository(_QueryRepository):
    def get(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT id, kind, component_count FROM artifacts WHERE id = ? LIMIT 1",
            (artifact_id,),
        )
        if row is None or row["kind"] != "composite":
            return None

        return {
            "artifact_id": str(row["id"]),
            "kind": str(row["kind"]),
            "component_count": row["component_count"],
            "membership_index": self.get_membership_index(artifact_id),
        }

    def get_components(self, artifact_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT id, composite_artifact_id, ordinal, relative_path, leaf_kind,
                   component_algorithm, component_digest, component_size,
                   component_type, artifact_id
            FROM composite_artifact_components
            WHERE composite_artifact_id = ?
            ORDER BY ordinal ASC, id ASC
            LIMIT ?
            """,
            (artifact_id, limit),
        )
        return [
            {
                "id": int(row["id"]),
                "composite_artifact_id": str(row["composite_artifact_id"]),
                "ordinal": row["ordinal"],
                "relative_path": row["relative_path"],
                "leaf_kind": row["leaf_kind"],
                "component_algorithm": row["component_algorithm"],
                "component_digest": row["component_digest"],
                "component_size": row["component_size"],
                "component_type": row["component_type"],
                "artifact_id": row["artifact_id"],
            }
            for row in rows
        ]

    def get_membership_index(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT total_components, stored_components, bloom_filter_base64, bloom_bits,
                   bloom_hashes, bloom_version
            FROM composite_membership_indexes
            WHERE composite_artifact_id = ?
            LIMIT 1
            """,
            (artifact_id,),
        )
        if row is None:
            return None

        return {
            "total_components": int(row["total_components"]),
            "stored_components": int(row["stored_components"]),
            "bloom_filter_base64": row["bloom_filter_base64"],
            "bloom_bits": row["bloom_bits"],
            "bloom_hashes": row["bloom_hashes"],
            "bloom_version": row["bloom_version"],
        }


def _session_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "hash": row["hash"],
        "created_at": row["created_at"],
        "source_artifact_hash": row["source_artifact_hash"],
        "current_step": row["current_step"],
        "is_active": row["is_active"],
        "git_repo": row["git_repo"],
        "git_commit_start": row["git_commit_start"],
        "git_commit_end": row["git_commit_end"],
        "synced_at": row["synced_at"],
        "metadata": row["metadata"],
    }


def _job_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "job_uid": row["job_uid"],
        "parent_job_uid": row["parent_job_uid"],
        "timestamp": row["timestamp"],
        "command": row["command"],
        "script": row["script"],
        "step_identity": row["step_identity"],
        "session_id": row["session_id"],
        "step_number": row["step_number"],
        "step_name": row["step_name"],
        "git_repo": row["git_repo"],
        "git_commit": row["git_commit"],
        "git_branch": row["git_branch"],
        "duration_seconds": row["duration_seconds"],
        "exit_code": row["exit_code"],
        "synced_at": row["synced_at"],
        "status": row["status"],
        "execution_backend": row["execution_backend"],
        "execution_role": row["execution_role"],
        "job_type": row["job_type"],
        "metadata": row["metadata"],
        "telemetry": row["telemetry"],
    }


def _artifact_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "size": int(row["size"] or 0),
        "first_seen_at": row["first_seen_at"],
        "first_seen_path": row["first_seen_path"],
        "source_type": row["source_type"],
        "source_url": row["source_url"],
        "uploaded_to": row["uploaded_to"],
        "synced_at": row["synced_at"],
        "kind": row["kind"],
        "component_count": row["component_count"],
        "metadata": row["metadata"],
    }


def create_query_database_context(roar_dir: Path) -> QueryDatabaseContext:
    """Create a lightweight query context for the given .roar directory."""
    return QueryDatabaseContext(roar_dir / "roar.db")
