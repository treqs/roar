"""P0-9: in a traced process tree, every process inherits one ROAR_LOG_FILE and
used to open it "w" — so a multiprocessing worker (litdata/DataLoader/HF datasets
num_proc/torchrun) could truncate the workload's record and be the one that
survived. write_log now writes a per-PID shard and merge_inject_logs unions them,
recovering the full workload instead of whichever process wrote last.
"""

from __future__ import annotations

import builtins
import json
import multiprocessing
import os

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
    merge_inject_logs,
)


class _FakeController:
    def handle_import(self, module_name, module):
        return None


class _FakeEnviron(dict):
    """A dict that tolerates the attribute patching ``install()`` performs."""


def _tracker(log_path):
    return RuntimeInjectionTracker(
        {"ROAR_LOG_FILE": str(log_path)},
        _FakeController(),
        log_file=str(log_path),
        inject_dir=str(log_path.parent / "inject"),
    )


def _shard(base, pid, data):
    (base.parent / f"{base.name}.{pid}").write_text(json.dumps(data), encoding="utf-8")


def _record_fork_only_import(tracker):
    tracker.imported_modules.add("fork_only_dependency")


def _record_then_force_exit(tracker):
    # Recorded after fork, so only an exit hook could capture it -- and
    # os._exit runs none.
    tracker.imported_modules.add("fork_only_dependency")
    os._exit(0)


def _installable_tracker(log_path):
    """A tracker whose environ tolerates ``install()``'s attribute patching."""
    return RuntimeInjectionTracker(
        _FakeEnviron({"ROAR_LOG_FILE": str(log_path)}),
        _FakeController(),
        log_file=str(log_path),
        inject_dir=str(log_path.parent / "inject"),
    )


def _report_pid(_):
    return os.getpid()


def _shards(tmp_path):
    return sorted(p.name for p in tmp_path.glob("inject-log.json.*"))


def test_install_is_what_wires_the_fork_worker_finalizer(tmp_path):
    """Go through ``install()``, not the private method.

    Calling ``_install_fork_worker_finalizer()`` directly passes even if the
    single line wiring it into ``install()`` is deleted -- and that line sits in
    the merge-conflict region with #287, so a conflict resolution could drop it
    silently. This test is the only thing that would notice.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        return

    log_path = tmp_path / "inject-log.json"
    tracker = _installable_tracker(log_path)

    saved_open, saved_import = builtins.open, builtins.__import__
    try:
        tracker.install()
        process = multiprocessing.get_context("fork").Process(
            target=_record_fork_only_import,
            args=(tracker,),
        )
        process.start()
        process.join(timeout=10)
    finally:
        builtins.open, builtins.__import__ = saved_open, saved_import

    assert process.exitcode == 0
    assert (tmp_path / f"inject-log.json.{process.pid}").exists()


def test_pool_context_manager_workers_still_report(tmp_path):
    """``with Pool(...)`` exits via ``terminate()``, which SIGTERMs the workers.

    SIGTERM's default disposition kills them outright, so neither the
    multiprocessing finalizer nor atexit runs. This is the common idiom -- and
    the ``num_proc`` case the finalizer's own docstring cites -- so it has to
    report, not just the ``close()``/``join()`` shape.

    Asserted against the workers that actually ran a task, since those
    demonstrably got through the after-fork hook. A worker forked and killed
    while still bootstrapping may write nothing: the eager write makes this
    best-effort, not a guarantee, and an exact shard count would be flaky.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        return

    log_path = tmp_path / "inject-log.json"
    tracker = _installable_tracker(log_path)
    tracker._install_fork_worker_finalizer()

    context = multiprocessing.get_context("fork")
    with context.Pool(2) as pool:
        worker_pids = set(pool.map(_report_pid, range(8)))

    assert worker_pids, "no worker ran a task"
    for pid in worker_pids:
        assert (tmp_path / f"inject-log.json.{pid}").exists(), (
            f"worker {pid} ran a task but never reported; shards: {_shards(tmp_path)}"
        )
    # The parent writes via atexit, which has not run yet.
    assert f"inject-log.json.{os.getpid()}" not in _shards(tmp_path)


def test_write_log_writes_a_per_pid_shard_not_the_shared_file(tmp_path):
    log_path = tmp_path / "inject-log.json"
    _tracker(log_path).write_log()
    assert not log_path.exists()  # the shared path is NOT truncated
    assert (tmp_path / f"inject-log.json.{os.getpid()}").exists()  # the shard is


def test_real_fork_worker_writes_its_own_pid_shard(tmp_path):
    """Linux multiprocessing fork workers use os._exit, so ordinary atexit does
    not run. The multiprocessing finalizer must write the worker's real shard."""
    if "fork" not in multiprocessing.get_all_start_methods():
        return

    log_path = tmp_path / "inject-log.json"
    tracker = _tracker(log_path)
    tracker._install_fork_worker_finalizer()
    process = multiprocessing.get_context("fork").Process(
        target=_record_fork_only_import,
        args=(tracker,),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    worker_shard = tmp_path / f"inject-log.json.{process.pid}"
    assert worker_shard.exists()
    payload = json.loads(worker_shard.read_text())
    assert payload["pid"] == process.pid
    assert "fork_only_dependency" in payload["imported_modules"]
    assert not (tmp_path / f"inject-log.json.{os.getpid()}").exists()


def test_a_forced_exit_keeps_the_fork_time_shard_but_loses_later_imports(tmp_path):
    """Document the lifecycle boundary precisely.

    A worker that calls ``os._exit`` bypasses multiprocessing cleanup as well as
    atexit, so no exit hook runs for it -- and the same is true of one killed by
    SIGTERM or SIGKILL. The eager write at fork means such a worker still
    contributes the state it inherited, rather than nothing at all.

    What is lost is what it imported *after* forking. That is the honest
    remainder, and closing it needs incremental import journaling rather than an
    exit hook. This test pins both halves so neither claim drifts.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        return

    log_path = tmp_path / "inject-log.json"
    tracker = _tracker(log_path)
    tracker._install_fork_worker_finalizer()
    process = multiprocessing.get_context("fork").Process(
        target=_record_then_force_exit,
        args=(tracker,),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    worker_shard = tmp_path / f"inject-log.json.{process.pid}"
    assert worker_shard.exists(), "the fork-time snapshot should survive a forced exit"
    payload = json.loads(worker_shard.read_text())
    assert "fork_only_dependency" not in payload["imported_modules"]


def test_worker_shard_does_not_clobber_the_workload_record(tmp_path):
    """MMA's litdata case: same command, a worker shard with argv ['-c'] and a
    subset of packages, plus the workload shard with the real command and the
    full set. The merge must keep the workload identity and union the packages."""
    base = tmp_path / "inject-log.json"
    # A litdata worker: sparse, argv ['-c'], few modules.
    _shard(
        base,
        222,
        {
            "argv": ["-c"],
            "modules_files": ["/sp/multiprocessing/spawn.py"],
            "used_packages": {"litdata": "0.2.59"},
            "imported_modules": ["litdata"],
            "opened_files": ["/data/shard-0.bin"],
            "installed_packages": {"litdata": "0.2.59"},
            "python_version": "3.12.10",
        },
    )
    # The workload: the real command, the full package set (torch/lightning/...).
    _shard(
        base,
        111,
        {
            "argv": ["train.py", "--epochs", "3"],
            "modules_files": [
                "/sp/torch/__init__.py",
                "/sp/lightning/__init__.py",
                "/repo/train.py",
            ],
            "used_packages": {"torch": "2.7.0", "lightning": "2.6.5", "litdata": "0.2.59"},
            "imported_modules": ["torch", "lightning", "litdata"],
            "opened_files": ["/repo/train.py"],
            "installed_packages": {"torch": "2.7.0", "lightning": "2.6.5", "litdata": "0.2.59"},
            "python_version": "3.12.10",
        },
    )

    merge_inject_logs(str(base))
    merged = json.loads(base.read_text(encoding="utf-8"))

    # Identity comes from the workload (richest shard), not the ['-c'] worker.
    assert merged["argv"] == ["train.py", "--epochs", "3"]
    # Packages/files/imports are the UNION across the tree.
    assert merged["used_packages"] == {"torch": "2.7.0", "lightning": "2.6.5", "litdata": "0.2.59"}
    assert set(merged["imported_modules"]) == {"torch", "lightning", "litdata"}
    assert (
        "/repo/train.py" in merged["opened_files"] and "/data/shard-0.bin" in merged["opened_files"]
    )
    # Shards are consumed.
    assert not list(tmp_path.glob("inject-log.json.*"))


def test_merge_prefers_a_concrete_version_over_none(tmp_path):
    base = tmp_path / "inject-log.json"
    _shard(base, 1, {"modules_files": ["/sp/a.py"], "used_packages": {"wandb": None}})
    _shard(
        base, 2, {"modules_files": ["/sp/a.py", "/sp/b.py"], "used_packages": {"wandb": "0.16.0"}}
    )
    merge_inject_logs(str(base))
    assert json.loads(base.read_text())["used_packages"]["wandb"] == "0.16.0"


def test_merge_is_noop_without_shards(tmp_path):
    base = tmp_path / "inject-log.json"
    merge_inject_logs(str(base))  # no shards on disk
    assert not base.exists()
