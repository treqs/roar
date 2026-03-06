from __future__ import annotations

import builtins

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_startup_complete", False)
    monkeypatch.setattr(roar_worker, "_actor_attribution_mode", "per_call")
    # Drain any leftover events from previous tests
    while not roar_worker._event_queue.empty():
        try:
            roar_worker._event_queue.get_nowait()
        except Exception:
            break


def test_log_read_pushes_event_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-1")

    roar_worker._log_read(
        path="/tmp/input.csv",
        hash_value=None,
        hash_algorithm="blake3",
        size=0,
        capture_method="python",
    )

    event = roar_worker._event_queue.get_nowait()
    assert event.kind == "read"
    assert event.task_id == "task-1"
    assert event.path == "/tmp/input.csv"
    assert event.capture_method == "python"


def test_log_write_pushes_event_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-2")

    roar_worker._log_write(
        path="/tmp/output.bin",
        hash_value="abc123",
        hash_algorithm="blake3",
        size=42,
        capture_method="python",
    )

    event = roar_worker._event_queue.get_nowait()
    assert event.kind == "write"
    assert event.task_id == "task-2"
    assert event.path == "/tmp/output.bin"
    assert event.hash_value == "abc123"
    assert event.size == 42


def test_tracking_open_hashes_written_bytes_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from blake3 import blake3

    import roar.ray.roar_worker as roar_worker

    output_path = tmp_path / "output.bin"
    payload = (b"checkpoint-data-" * 32) + b"end"

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-9")
    monkeypatch.setattr(roar_worker, "_should_track_local_path", lambda _path: True)

    handle = roar_worker._tracking_open(output_path, "wb")
    handle.write(payload[:100])
    handle.write(payload[100:])
    handle.close()

    # Write event should be in the queue
    event = roar_worker._event_queue.get_nowait()
    assert event.kind == "write"
    assert event.path == str(output_path.resolve())
    assert event.hash_algorithm == "blake3"
    assert event.hash_value == blake3(payload).hexdigest()
    assert event.size == len(payload)


def test_get_task_and_actor_id_do_not_import_ray_during_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.delitem(roar_worker.sys.modules, "ray", raising=False)

    real_import = builtins.__import__

    def _guard_import(name, *args, **kwargs):
        if name == "ray":
            raise AssertionError("ray import should not be attempted")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard_import)

    # _get_current_task_id calls _get_task_id internally
    assert roar_worker._get_current_task_id() == ""
    assert roar_worker._get_actor_id() is None


def test_start_fragment_uses_task_function_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setenv("ROAR_JOB_ID", "job-abc")
    monkeypatch.setattr(roar_worker, "_get_task_function_name", lambda: "ingest_shard")

    fragment = roar_worker._start_fragment("task-xyz")
    assert fragment.function_name == "ingest_shard"


def test_parse_proxy_log_lines_extracts_s3_ops() -> None:
    import roar.ray.roar_worker as roar_worker

    lines = [
        '[S3:PutObject] s3://test-bucket/data/file.csv  (1234 bytes)  etag="abc123"',
        '[S3:GetObject] s3://test-bucket/data/file.csv  (5678 bytes)  etag="def456"',
        "[S3:HeadObject] s3://test-bucket/data/file.csv",
        "[S3:CreateMultipartUpload] s3://test-bucket/big/file.bin",
        "some non-matching line",
    ]

    results = roar_worker._parse_proxy_log_lines(lines)

    assert len(results) == 3  # CreateMultipartUpload and non-matching skipped

    kind0, ref0 = results[0]
    assert kind0 == "write"
    assert ref0.path == "s3://test-bucket/data/file.csv"
    assert ref0.size == 1234
    assert ref0.hash == "abc123"
    assert ref0.capture_method == "proxy"

    kind1, ref1 = results[1]
    assert kind1 == "read"
    assert ref1.path == "s3://test-bucket/data/file.csv"
    assert ref1.size == 5678

    kind2, ref2 = results[2]
    assert kind2 == "read"
    assert ref2.path == "s3://test-bucket/data/file.csv"
    assert ref2.size == 0  # HeadObject has no size


def test_main_calls_startup_and_runs_worker_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    startup_calls: list[bool] = []
    monkeypatch.setattr(roar_worker, "_startup", lambda: startup_calls.append(True))

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        roar_worker,
        "_run_worker_entrypoint",
        lambda argv: captured.setdefault("argv", argv),
    )
    monkeypatch.setattr(roar_worker.sys, "argv", ["roar-worker", "-u", "worker.py"])

    roar_worker.main()

    assert startup_calls == [True]
    assert captured["argv"] == ["-u", "worker.py"]


def test_run_worker_entrypoint_execs_python_for_non_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    captured: dict[str, object] = {}

    def _fake_execvp(program: str, argv: list[str]) -> None:
        captured["program"] = program
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(roar_worker.os, "execvp", _fake_execvp)

    with pytest.raises(SystemExit):
        roar_worker._run_worker_entrypoint(["-u", "worker.py"])

    assert captured["program"] == "python3"
    assert captured["argv"] == ["python3", "-u", "worker.py"]


def test_run_worker_entrypoint_execs_python_for_worker_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    captured: dict[str, object] = {}

    def _fake_execvp(program: str, argv: list[str]) -> None:
        captured["program"] = program
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(roar_worker.os, "execvp", _fake_execvp)

    with pytest.raises(SystemExit):
        roar_worker._run_worker_entrypoint(
            ["/tmp/default_worker.py", "--worker-type", "RAY_WORKER"]
        )

    assert captured["program"] == "python3"
    assert captured["argv"] == ["python3", "/tmp/default_worker.py", "--worker-type", "RAY_WORKER"]
