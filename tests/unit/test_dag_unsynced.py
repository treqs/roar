"""Coverage for `session_has_unsynced`, which gates the `roar dag` persist hint.

A job's ``synced_at`` is ``NULL`` until it's registered to GLaaS, so the
"save lineage" nudge on `roar dag` should appear only while something is
still local-only, and stop once everything is registered.
"""

from __future__ import annotations

from pathlib import Path

from roar.application.query.dag import session_has_unsynced
from roar.db.context import create_database_context


def _session_with_one_job(roar_dir: Path) -> int:
    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        job_id, _job_uid = db_ctx.jobs.create(
            command="echo hi",
            timestamp=1700000000.0,
            session_id=session_id,
            step_number=1,
        )
        return job_id


def test_has_unsynced_true_for_fresh_job(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    _session_with_one_job(roar_dir)
    assert session_has_unsynced(roar_dir) is True


def test_has_unsynced_false_after_mark_synced(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    job_id = _session_with_one_job(roar_dir)
    with create_database_context(roar_dir) as db_ctx:
        db_ctx.jobs.mark_synced([job_id], 1700000001.0)
    assert session_has_unsynced(roar_dir) is False


def test_has_unsynced_false_without_session(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    # Initialize the schema but create no session.
    with create_database_context(roar_dir):
        pass
    assert session_has_unsynced(roar_dir) is False
