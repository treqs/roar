"""Unit tests for active-run marker write/list/cleanup behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path

from roar.execution.runtime.active_runs import (
    active_run_marker,
    list_active_runs,
    remove_marker,
    write_marker,
)


def _markers_dir(roar_dir: Path) -> Path:
    return roar_dir / "active_runs"


def test_write_marker_creates_file_with_expected_fields(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    write_marker(roar_dir, pid=1234, command=["python", "train.py"], job_type="run")

    marker_path = _markers_dir(roar_dir) / "1234.json"
    assert marker_path.exists()
    info = json.loads(marker_path.read_text())
    assert info["pid"] == 1234
    assert info["command"] == ["python", "train.py"]
    assert info["job_type"] == "run"
    assert isinstance(info["started_at"], float)


def test_write_marker_is_best_effort_on_unwritable_dir(tmp_path: Path) -> None:
    """A path component blocked by a file (not a dir) must not raise."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    write_marker(blocker / ".roar", pid=1, command=["x"], job_type="run")  # must not raise


def test_remove_marker_is_best_effort_when_missing(tmp_path: Path) -> None:
    remove_marker(tmp_path / ".roar", pid=999)  # must not raise, no file exists


def test_active_run_marker_cleans_up_on_normal_exit(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    with active_run_marker(roar_dir, pid=42, command=["x"], job_type="run"):
        assert (_markers_dir(roar_dir) / "42.json").exists()
    assert not (_markers_dir(roar_dir) / "42.json").exists()


def test_active_run_marker_cleans_up_on_exception(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    try:
        with active_run_marker(roar_dir, pid=43, command=["x"], job_type="build"):
            assert (_markers_dir(roar_dir) / "43.json").exists()
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not (_markers_dir(roar_dir) / "43.json").exists()


def test_list_active_runs_returns_empty_when_no_markers_dir(tmp_path: Path) -> None:
    assert list_active_runs(tmp_path / ".roar") == []


def test_list_active_runs_reports_live_pid(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    # Our own PID is guaranteed alive for the duration of the test.
    write_marker(roar_dir, pid=os.getpid(), command=["python", "train.py"], job_type="run")

    active = list_active_runs(roar_dir)

    assert len(active) == 1
    assert active[0]["pid"] == os.getpid()
    # Still on disk — a live PID's marker is not touched.
    assert (_markers_dir(roar_dir) / f"{os.getpid()}.json").exists()


def test_list_active_runs_self_heals_dead_pid(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    # PID 2**30 is astronomically unlikely to be a live process.
    dead_pid = 2**30
    write_marker(roar_dir, pid=dead_pid, command=["python", "train.py"], job_type="run")

    active = list_active_runs(roar_dir)

    assert active == []
    assert not (_markers_dir(roar_dir) / f"{dead_pid}.json").exists()


def test_list_active_runs_self_heals_corrupt_marker(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    markers_dir = _markers_dir(roar_dir)
    markers_dir.mkdir(parents=True)
    (markers_dir / "garbage.json").write_text("{not json")

    active = list_active_runs(roar_dir)

    assert active == []
    assert not (markers_dir / "garbage.json").exists()
