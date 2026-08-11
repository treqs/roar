"""W1: durable pending-publication markers survive a crash and are recoverable."""

from __future__ import annotations

from roar.application.publish.pending_publication import (
    clear_pending,
    list_pending,
    write_pending,
)


def test_write_then_list_then_clear(tmp_path):
    roar_dir = tmp_path / ".roar"
    write_pending(
        roar_dir,
        session_hash="abc123",
        registration_session_id="sess-1",
        mode="anonymous_public",
        now=1000.0,
    )
    pending = list_pending(roar_dir)
    assert len(pending) == 1
    assert pending[0]["session_hash"] == "abc123"
    assert pending[0]["registration_session_id"] == "sess-1"
    assert pending[0]["started_epoch"] == 1000.0

    clear_pending(roar_dir, "abc123")
    assert list_pending(roar_dir) == []


def test_clear_is_idempotent_and_never_raises(tmp_path):
    # clearing a marker that was never written must not raise
    clear_pending(tmp_path / ".roar", "never-written")


def test_list_on_missing_dir_is_empty(tmp_path):
    assert list_pending(tmp_path / "nope") == []


def test_multiple_pending_are_all_listed(tmp_path):
    roar_dir = tmp_path / ".roar"
    for h in ("h1", "h2", "h3"):
        write_pending(roar_dir, session_hash=h, registration_session_id=None)
    assert {m["session_hash"] for m in list_pending(roar_dir)} == {"h1", "h2", "h3"}


def test_unreadable_marker_is_skipped_not_fatal(tmp_path):
    roar_dir = tmp_path / ".roar"
    write_pending(roar_dir, session_hash="good", registration_session_id=None)
    # a torn/garbage marker alongside a good one must not break listing
    (roar_dir / "pending-publications" / "garbage.json").write_text("{not json")
    assert [m["session_hash"] for m in list_pending(roar_dir)] == ["good"]


def test_write_never_raises_on_bad_path():
    # writing under an impossible location is swallowed (durability is best-effort)
    write_pending("/proc/nonexistent/cannot/mkdir", session_hash="x", registration_session_id=None)
