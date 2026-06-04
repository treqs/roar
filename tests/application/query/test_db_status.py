"""Tests for the local DB status collector + renderer (`roar db`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.application.query.db_status import (
    DbStatusQueryRequest,
    collect_db_status,
    render_db_status,
)

_DAY = 86_400


def _build_db(path: Path, now: float) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 3")
    conn.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, created_at REAL, synced_at REAL);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, timestamp REAL, synced_at REAL,
                           step_identity TEXT);
        CREATE TABLE artifacts (id TEXT PRIMARY KEY, size INTEGER, first_seen_at REAL,
                                synced_at REAL, kind TEXT, component_count INTEGER);
        CREATE TABLE labels (id INTEGER PRIMARY KEY, synced_at REAL);
        CREATE TABLE job_inputs (job_id INTEGER, artifact_id TEXT, path TEXT);
        CREATE TABLE job_outputs (job_id INTEGER, artifact_id TEXT, path TEXT);
        CREATE TABLE composite_artifact_components (id INTEGER PRIMARY KEY,
                                                    composite_artifact_id TEXT);
        CREATE TABLE collections (id INTEGER PRIMARY KEY);
        """
    )
    # 2 sessions, 1 synced
    conn.execute("INSERT INTO sessions VALUES (1, ?, ?)", (now - 100 * _DAY, now))
    conn.execute("INSERT INTO sessions VALUES (2, ?, NULL)", (now,))
    # 3 jobs: two share step_identity 'A' (one re-run), one 'B' -> superseded = 1
    conn.execute("INSERT INTO jobs VALUES (1, ?, ?, 'A')", (now, now))
    conn.execute("INSERT INTO jobs VALUES (2, ?, NULL, 'A')", (now,))
    conn.execute("INSERT INTO jobs VALUES (3, ?, NULL, 'B')", (now,))
    # 4 artifacts across age buckets; a4 is composite and orphaned (no job link)
    conn.execute(
        "INSERT INTO artifacts VALUES ('a1', 10, ?, ?, 'primitive', NULL)", (now - 1 * _DAY, now)
    )
    conn.execute("INSERT INTO artifacts VALUES ('a2', 20, ?, NULL, NULL, NULL)", (now - 10 * _DAY,))
    conn.execute(
        "INSERT INTO artifacts VALUES ('a3', 30, ?, NULL, 'primitive', NULL)", (now - 50 * _DAY,)
    )
    conn.execute(
        "INSERT INTO artifacts VALUES ('a4', 40, ?, NULL, 'composite', 6)", (now - 100 * _DAY,)
    )
    conn.executemany(
        "INSERT INTO composite_artifact_components VALUES (?, 'a4')", [(i,) for i in range(6)]
    )
    conn.execute("INSERT INTO labels VALUES (1, ?)", (now,))
    conn.execute("INSERT INTO labels VALUES (2, NULL)")
    # link a1, a2, a3 (a4 stays orphan)
    conn.execute("INSERT INTO job_inputs VALUES (1, 'a1', '/a1')")
    conn.execute("INSERT INTO job_inputs VALUES (1, 'a2', '/a2')")
    conn.execute("INSERT INTO job_outputs VALUES (1, 'a3', '/a3')")
    conn.commit()
    conn.close()


def test_collect_db_status_counts_and_buckets(tmp_path: Path) -> None:
    now = 1_780_000_000.0
    db = tmp_path / "roar.db"
    _build_db(db, now)

    s = collect_db_status(db, now=now)

    assert s.exists is True
    assert s.schema_version == 3
    assert s.counts["sessions"] == 2
    assert s.counts["jobs"] == 3
    assert s.counts["artifacts"] == 4
    assert s.counts["artifacts_composite"] == 1
    assert s.counts["artifacts_primitive"] == 3
    assert s.counts["components"] == 6
    assert s.counts["labels"] == 2
    assert s.counts["links_in"] == 2
    assert s.counts["links_out"] == 1
    assert s.counts["links"] == 3

    assert s.sync["sessions"].synced == 1 and s.sync["sessions"].pct == 50
    assert s.sync["jobs"].synced == 1 and s.sync["jobs"].unsynced == 2
    assert s.sync["artifacts"].synced == 1
    assert s.sync["labels"].synced == 1

    assert s.orphan_artifacts == 1  # a4
    assert s.superseded_jobs == 1  # one extra 'A'

    assert s.age_buckets == {"<=7d": 1, "7-30d": 1, "30-90d": 1, ">90d": 1}
    assert s.oldest == now - 100 * _DAY
    assert s.newest == now


def test_empty_pct_for_zero_labels(tmp_path: Path) -> None:
    now = 1_780_000_000.0
    db = tmp_path / "roar.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE labels (id INTEGER PRIMARY KEY, synced_at REAL)")
    conn.commit()
    conn.close()
    s = collect_db_status(db, now=now)
    assert s.sync["labels"].pct is None  # rendered as "—"


def test_missing_database_renders_init_hint(tmp_path: Path) -> None:
    req = DbStatusQueryRequest(roar_dir=tmp_path)
    out = render_db_status(req)
    assert "No local database" in out
    assert "roar init" in out


def test_render_includes_sections(tmp_path: Path) -> None:
    now = 1_780_000_000.0
    (tmp_path / "roar.db").touch()
    _build_db(tmp_path / "roar.db", now)
    out = render_db_status(DbStatusQueryRequest(roar_dir=tmp_path))
    for header in ("Database:", "Contents:", "Synced to GLaaS:", "Hygiene:", "Artifact age:"):
        assert header in out
    assert "Orphan artifacts:        1" in out
