from __future__ import annotations

import builtins
import io

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_current_task_id", None)
    monkeypatch.setattr(roar_worker, "_current_fragment", None)
    monkeypatch.setattr(roar_worker, "_fragment_streamer", None)
    monkeypatch.setattr(roar_worker, "_startup_complete", False)
    monkeypatch.setattr(roar_worker, "_actor_attribution_mode", "per_call")


def test_check_task_boundary_rotates_fragments_when_task_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    task_ids = iter(["task-1", "task-1", "task-2"])
    monkeypatch.setattr(roar_worker, "_get_task_id", lambda: next(task_ids))

    started: list[str] = []
    finalised: list[str] = []

    def _start_fragment(task_id: str):
        started.append(task_id)
        return {"task_id": task_id}

    def _finalise_fragment(fragment: dict) -> None:
        finalised.append(fragment["task_id"])

    monkeypatch.setattr(roar_worker, "_start_fragment", _start_fragment)
    monkeypatch.setattr(roar_worker, "_finalise_fragment", _finalise_fragment)

    roar_worker._check_task_boundary()
    assert started == ["task-1"]
    assert finalised == []

    roar_worker._check_task_boundary()
    assert started == ["task-1"]
    assert finalised == []

    roar_worker._check_task_boundary()
    assert started == ["task-1", "task-2"]
    assert finalised == ["task-1"]


def test_tracking_open_hashes_written_bytes_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from blake3 import blake3

    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    output_path = tmp_path / "output.bin"
    payload = (b"checkpoint-data-" * 32) + b"end"

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)
    monkeypatch.setattr(roar_worker, "_should_track_local_path", lambda _path: True)

    handle = roar_worker._tracking_open(output_path, "wb")
    handle.write(payload[:100])
    handle.write(payload[100:])
    handle.close()

    assert len(fragment.writes) == 1
    write_ref = fragment.writes[0]
    assert write_ref.path == str(output_path.resolve())
    assert write_ref.hash_algorithm == "blake3"
    assert write_ref.hash == blake3(payload).hexdigest()
    assert write_ref.size == len(payload)


def test_log_write_emits_fragment_snapshot_on_each_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="emit1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker.time, "time", lambda: 42.0)

    emitted: list[dict] = []
    monkeypatch.setattr(
        roar_worker,
        "_emit_fragment",
        lambda value: emitted.append(value.to_dict()),
    )

    roar_worker._log_write(
        path="/tmp/out.bin",
        hash_value="abc123",
        hash_algorithm="blake3",
        size=3,
        capture_method="python",
    )

    assert emitted
    assert emitted[0]["job_uid"] == "emit1234"
    assert emitted[0]["writes"][0]["path"] == "/tmp/out.bin"
    assert fragment.ended_at == 42.0


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

    assert roar_worker._get_task_id() is None
    assert roar_worker._get_actor_id() is None


def test_wrap_s3_client_logs_etag_on_put_object(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    class _FakeS3Client:
        @staticmethod
        def put_object(*args, **kwargs):
            del args, kwargs
            return {"ETag": '"etag-value-123"'}

    wrapped = roar_worker._wrap_s3_client(_FakeS3Client())
    wrapped.put_object(Bucket="demo-bucket", Key="path/to/object.bin", Body=b"payload")

    assert len(fragment.writes) == 1
    write_ref = fragment.writes[0]
    assert write_ref.path == "s3://demo-bucket/path/to/object.bin"
    assert write_ref.hash_algorithm == "etag"
    assert write_ref.hash == "etag-value-123"
    assert write_ref.size == len(b"payload")
    assert write_ref.capture_method == "proxy"


def test_wrap_s3_client_put_object_uses_size_for_empty_bytes_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    class _FakeS3Client:
        @staticmethod
        def put_object(*args, **kwargs):
            del args, kwargs
            return {"ETag": '"etag-value-123"'}

    wrapped = roar_worker._wrap_s3_client(_FakeS3Client())
    wrapped.put_object(Bucket="demo-bucket", Key="path/to/object.bin", Body=b"")

    assert len(fragment.writes) == 1
    assert fragment.writes[0].size == 0


def test_wrap_s3_client_put_object_uses_size_for_seekable_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    class _FakeS3Client:
        @staticmethod
        def put_object(*args, **kwargs):
            del args, kwargs
            return {"ETag": '"etag-value-123"'}

    body = io.BytesIO(b"hello world")
    wrapped = roar_worker._wrap_s3_client(_FakeS3Client())
    wrapped.put_object(Bucket="demo-bucket", Key="path/to/object.bin", Body=body)

    assert len(fragment.writes) == 1
    assert fragment.writes[0].size == 11
    assert body.tell() == 0


def test_wrap_s3_client_upload_file_uses_local_file_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    payload = b"upload-file-payload"
    local_file = tmp_path / "upload.bin"
    local_file.write_bytes(payload)

    class _FakeS3Client:
        @staticmethod
        def upload_file(*args, **kwargs):
            del args, kwargs
            return None

    wrapped = roar_worker._wrap_s3_client(_FakeS3Client())
    wrapped.upload_file(str(local_file), "demo-bucket", "path/to/object.bin")

    assert len(fragment.writes) == 1
    write_ref = fragment.writes[0]
    assert write_ref.path == "s3://demo-bucket/path/to/object.bin"
    assert write_ref.size == len(payload)
    assert write_ref.capture_method == "proxy"


def test_wrap_s3_client_logs_etag_on_get_object(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    class _FakeS3Client:
        @staticmethod
        def get_object(*args, **kwargs):
            del args, kwargs
            return {
                "ETag": '"etag-read-123"',
                "ContentLength": 4,
                "Body": io.BytesIO(b"data"),
            }

    wrapped = roar_worker._wrap_s3_client(_FakeS3Client())
    wrapped.get_object(Bucket="demo-bucket", Key="path/to/object.bin")

    assert len(fragment.reads) == 1
    read_ref = fragment.reads[0]
    assert read_ref.path == "s3://demo-bucket/path/to/object.bin"
    assert read_ref.hash_algorithm == "etag"
    assert read_ref.hash == "etag-read-123"
    assert read_ref.capture_method == "proxy"


def test_start_fragment_uses_task_function_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setenv("ROAR_JOB_ID", "job-abc")
    monkeypatch.setattr(roar_worker, "_get_task_function_name", lambda: "ingest_shard")

    fragment = roar_worker._start_fragment("task-xyz")
    assert fragment.function_name == "ingest_shard"


def test_actor_attribution_per_call_uses_task_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    task_ids = iter(["task-1", "task-2"])
    monkeypatch.setattr(roar_worker, "_get_task_id", lambda: next(task_ids))
    monkeypatch.setattr(roar_worker, "_get_actor_id", lambda: "actor-1")
    monkeypatch.setattr(roar_worker, "_actor_attribution_mode", "per_call")

    started: list[str] = []
    finalised: list[str] = []

    def _start_fragment(boundary_id: str):
        started.append(boundary_id)
        return {"boundary_id": boundary_id}

    def _finalise_fragment(fragment: dict) -> None:
        finalised.append(fragment["boundary_id"])

    monkeypatch.setattr(roar_worker, "_start_fragment", _start_fragment)
    monkeypatch.setattr(roar_worker, "_finalise_fragment", _finalise_fragment)

    roar_worker._check_task_boundary()
    roar_worker._check_task_boundary()

    assert started == ["task-1", "task-2"]
    assert finalised == ["task-1"]


def test_actor_attribution_per_actor_groups_calls_under_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    task_ids = iter(["task-1", "task-2"])
    monkeypatch.setattr(roar_worker, "_get_task_id", lambda: next(task_ids))
    monkeypatch.setattr(roar_worker, "_get_actor_id", lambda: "actor-1")
    monkeypatch.setattr(roar_worker, "_actor_attribution_mode", "per_actor")

    started: list[str] = []
    finalised: list[str] = []

    def _start_fragment(boundary_id: str):
        started.append(boundary_id)
        return {"boundary_id": boundary_id}

    def _finalise_fragment(fragment: dict) -> None:
        finalised.append(fragment["boundary_id"])

    monkeypatch.setattr(roar_worker, "_start_fragment", _start_fragment)
    monkeypatch.setattr(roar_worker, "_finalise_fragment", _finalise_fragment)

    roar_worker._check_task_boundary()
    roar_worker._check_task_boundary()

    assert started == ["actor-1"]
    assert finalised == []


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


def test_finalise_fragment_emits_fragment_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    emitted: list[dict] = []

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(
        roar_worker,
        "_emit_fragment",
        lambda payload: emitted.append(payload.to_dict()),
    )
    monkeypatch.setattr(roar_worker.time, "time", lambda: 10.0)

    roar_worker._finalise_fragment(fragment)

    assert emitted
    assert emitted[0]["job_uid"] == "abcd1234"
    assert emitted[0]["ended_at"] == 10.0


def test_flush_current_fragment_finalises_last_task_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import TaskFragment

    fragment = TaskFragment(
        job_uid="flush1234",
        parent_job_uid="parent123",
        ray_task_id="task-9",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="train",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )
    monkeypatch.setattr(roar_worker, "_current_fragment", fragment)
    monkeypatch.setattr(roar_worker, "_current_task_id", "task-9")

    finalised: list[str] = []
    monkeypatch.setattr(
        roar_worker,
        "_finalise_fragment",
        lambda value: finalised.append(value.job_uid),
    )

    roar_worker._flush_current_fragment()

    assert finalised == ["flush1234"]
    assert roar_worker._current_fragment is None
    assert roar_worker._current_task_id is None
