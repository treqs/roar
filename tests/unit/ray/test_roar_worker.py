from __future__ import annotations

import builtins
import contextlib
import sys
import time
import types
from collections import namedtuple
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    roar_worker._shutdown_collector()
    monkeypatch.setattr(roar_worker, "_startup_complete", False)
    monkeypatch.setattr(roar_worker, "_actor_attribution_mode", "per_call")
    monkeypatch.setattr(roar_worker, "_collector_thread", None)
    monkeypatch.setattr(roar_worker, "_direct_streamer", None)
    monkeypatch.setattr(roar_worker, "_proxy_configured", False)
    builtins.open = roar_worker._real_open
    monkeypatch.setattr(roar_worker.threading.Thread, "start", roar_worker._real_thread_start)
    monkeypatch.setattr(roar_worker, "_native_threading_patch_refcount", 0)
    with contextlib.suppress(AttributeError):
        delattr(roar_worker._native_task_launch_context, "task_id")
    roar_worker._shutdown_event.clear()
    with roar_worker._native_lock:
        roar_worker._native_events_buffer.clear()
    with roar_worker._native_child_task_lock:
        roar_worker._native_child_task_ids.clear()
    with roar_worker._native_thread_task_lock:
        roar_worker._native_thread_task_ids.clear()
        roar_worker._recent_native_thread_task_ids.clear()
    while not roar_worker._event_queue.empty():
        try:
            roar_worker._event_queue.get_nowait()
        except Exception:
            break
    yield
    builtins.open = roar_worker._real_open
    monkeypatch.setattr(roar_worker.threading.Thread, "start", roar_worker._real_thread_start)
    monkeypatch.setattr(roar_worker, "_native_threading_patch_refcount", 0)
    with contextlib.suppress(AttributeError):
        delattr(roar_worker._native_task_launch_context, "task_id")
    roar_worker._shutdown_collector()
    roar_worker._shutdown_event.clear()
    with roar_worker._native_lock:
        roar_worker._native_events_buffer.clear()
    with roar_worker._native_child_task_lock:
        roar_worker._native_child_task_ids.clear()
    with roar_worker._native_thread_task_lock:
        roar_worker._native_thread_task_ids.clear()
        roar_worker._recent_native_thread_task_ids.clear()
    while not roar_worker._event_queue.empty():
        try:
            roar_worker._event_queue.get_nowait()
        except Exception:
            break


def test_log_read_pushes_event_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-1")
    monkeypatch.setattr(roar_worker, "_get_task_function_name", lambda: "read_task")

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
    assert event.function_name == "read_task"
    assert event.path == "/tmp/input.csv"
    assert event.capture_method == "python"


def test_log_write_pushes_event_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-2")
    monkeypatch.setattr(roar_worker, "_get_task_function_name", lambda: "write_task")

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
    assert event.function_name == "write_task"
    assert event.path == "/tmp/output.bin"
    assert event.hash_value == "abc123"
    assert event.size == 42


def test_track_s3_api_call_uses_captured_task_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        roar_worker,
        "_log_write",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(roar_worker, "_resolved_task_id", lambda: "")
    monkeypatch.setattr(roar_worker, "_get_task_function_name", lambda: "unknown")

    roar_worker._track_s3_api_call(
        "PutObject",
        {"Bucket": "test-bucket", "Key": "snap.txt", "Body": b"hello"},
        {"ETag": '"etag-1"'},
        task_id="task-snap",
        function_name="write_s3",
    )

    assert captured["path"] == "s3://test-bucket/snap.txt"
    assert captured["task_id"] == "task-snap"
    assert captured["function_name"] == "write_s3"


def test_tracking_open_hashes_written_bytes_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    assert len(results) == 3

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
    assert ref2.size == 0


def test_configure_local_proxy_endpoint_preserves_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setenv("ROAR_UPSTREAM_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:19191")
    monkeypatch.setenv("ROAR_PROXY_PORT", "19191")

    roar_worker._configure_local_proxy_endpoint()

    assert roar_worker.os.environ["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://minio:9000"
    assert roar_worker.os.environ["AWS_ENDPOINT_URL"] == "http://127.0.0.1:19191"
    assert roar_worker.os.environ["ROAR_PROXY_PORT"] == "19191"
    assert roar_worker._proxy_configured is True


def test_configure_local_proxy_endpoint_captures_original_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.delenv("ROAR_UPSTREAM_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("ROAR_PROXY_PORT", raising=False)

    roar_worker._configure_local_proxy_endpoint()

    assert roar_worker.os.environ["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://minio:9000"
    assert roar_worker.os.environ["AWS_ENDPOINT_URL"] == "http://127.0.0.1:19191"
    assert roar_worker.os.environ["ROAR_PROXY_PORT"] == "19191"
    assert roar_worker._proxy_configured is True


def test_collector_flushes_local_events_after_short_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    flushed: list[dict[str, object]] = []

    class _FakeStreamer:
        def __init__(
            self,
            *,
            session_id: str,
            token: str,
            glaas_url: str,
            sequence_start: int = 0,
            sequence_step: int = 1,
        ) -> None:
            self.session_id = session_id
            self.token = token
            self.glaas_url = glaas_url
            self.sequence_start = sequence_start
            self.sequence_step = sequence_step
            self._pending: list[dict[str, object]] = []

        def append_fragment(self, fragment_dict: dict[str, object]) -> None:
            self._pending.append(fragment_dict)

        def flush(self) -> bool:
            flushed.extend(self._pending)
            self._pending.clear()
            return True

        def close(self) -> None:
            return None

    monkeypatch.setenv("ROAR_SESSION_ID", "session-1")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ab" * 32)
    monkeypatch.setenv("GLAAS_URL", "http://localhost:3001")
    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _FakeStreamer)
    monkeypatch.setattr(roar_worker, "_FLUSH_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(roar_worker, "_IDLE_FLUSH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(roar_worker, "_FLUSH_THRESHOLD_EVENTS", 200)
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-local")

    roar_worker._start_collector()
    roar_worker._log_write(
        path="/tmp/output.bin",
        hash_value="deadbeef",
        hash_algorithm="blake3",
        size=8,
        capture_method="python",
    )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not flushed:
        time.sleep(0.01)

    roar_worker._shutdown_collector()

    assert flushed, "collector did not flush local event after going idle"
    fragment = flushed[0]
    assert fragment["ray_task_id"] == "task-local"
    assert fragment["writes"][0]["path"] == "/tmp/output.bin"


def test_collector_lazily_initializes_streamer_from_worker_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    appended: list[dict[str, object]] = []

    class _FakeStreamer:
        def __init__(
            self,
            *,
            session_id: str,
            token: str,
            glaas_url: str,
            sequence_start: int = 0,
            sequence_step: int = 1,
        ) -> None:
            self.session_id = session_id
            self.token = token
            self.glaas_url = glaas_url
            self.sequence_start = sequence_start
            self.sequence_step = sequence_step

        def append_fragment(self, fragment_dict: dict[str, object]) -> None:
            appended.append(fragment_dict)

        def flush(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.delenv("ROAR_SESSION_ID", raising=False)
    monkeypatch.delenv("ROAR_FRAGMENT_TOKEN", raising=False)
    monkeypatch.delenv("GLAAS_URL", raising=False)
    monkeypatch.delenv("GLAAS_API_URL", raising=False)
    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _FakeStreamer)
    monkeypatch.setattr(roar_worker, "_FLUSH_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(roar_worker, "_IDLE_FLUSH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(roar_worker, "_FLUSH_THRESHOLD_EVENTS", 200)
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-lazy")

    roar_worker._start_collector()

    monkeypatch.setenv("ROAR_SESSION_ID", "session-lazy")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ef" * 32)
    monkeypatch.setenv("GLAAS_URL", "http://localhost:3001")
    roar_worker._log_write(
        path="/tmp/lazy.bin",
        hash_value="cafebabe",
        hash_algorithm="blake3",
        size=4,
        capture_method="python",
    )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not appended:
        time.sleep(0.01)

    roar_worker._shutdown_collector()

    assert appended, "collector did not initialize streamer after env vars appeared"
    fragment = appended[0]
    assert fragment["ray_task_id"] == "task-lazy"
    assert fragment["writes"][0]["path"] == "/tmp/lazy.bin"


def test_log_write_eagerly_flushes_local_event_when_streamer_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    flushed: list[dict[str, object]] = []

    class _FakeStreamer:
        def append_fragment(self, fragment_dict: dict[str, object]) -> None:
            flushed.append(fragment_dict)

        def flush(self) -> bool:
            return True

    monkeypatch.setattr(roar_worker, "_ensure_direct_streamer", lambda: _FakeStreamer())
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-eager")
    monkeypatch.setenv("ROAR_JOB_ID", "job-eager")

    roar_worker._log_write(
        path="/tmp/eager.bin",
        hash_value="facefeed",
        hash_algorithm="blake3",
        size=12,
        capture_method="python",
    )

    assert flushed, "local event was not eagerly flushed"
    fragment = flushed[0]
    assert fragment["ray_task_id"] == "task-eager"
    assert fragment["writes"][0]["path"] == "/tmp/eager.bin"


def test_collector_flushes_native_entries_with_current_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import ArtifactRef

    appended: list[dict[str, object]] = []

    class _FakeStreamer:
        def __init__(
            self,
            *,
            session_id: str,
            token: str,
            glaas_url: str,
            sequence_start: int = 0,
            sequence_step: int = 1,
        ) -> None:
            self.session_id = session_id
            self.token = token
            self.glaas_url = glaas_url
            self.sequence_start = sequence_start
            self.sequence_step = sequence_step

        def append_fragment(self, fragment_dict: dict[str, object]) -> None:
            appended.append(fragment_dict)

        def flush(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setenv("ROAR_SESSION_ID", "session-native")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "cd" * 32)
    monkeypatch.setenv("GLAAS_URL", "http://localhost:3001")
    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _FakeStreamer)
    monkeypatch.setattr(roar_worker, "_FLUSH_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(roar_worker, "_IDLE_FLUSH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(roar_worker, "_FLUSH_THRESHOLD_EVENTS", 200)
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-native")

    roar_worker._start_collector()
    with roar_worker._native_lock:
        roar_worker._native_events_buffer.append(
            (
                "",
                "write",
                ArtifactRef(
                    path="/tmp/native-output.bin",
                    hash=None,
                    hash_algorithm="",
                    size=0,
                    capture_method="native",
                ),
            )
        )


    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not appended:
        time.sleep(0.01)

    roar_worker._shutdown_collector()

    assert appended, "collector did not flush native entries after going idle"
    fragment = appended[0]
    assert fragment["ray_task_id"] == "task-native"
    assert fragment["writes"][0]["path"] == "/tmp/native-output.bin"
    assert fragment["writes"][0]["capture_method"] == "native"


def test_collector_prefers_bound_task_id_for_native_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker
    from roar.ray.fragment import ArtifactRef

    appended: list[dict[str, object]] = []

    class _FakeStreamer:
        def __init__(
            self,
            *,
            session_id: str,
            token: str,
            glaas_url: str,
            sequence_start: int = 0,
            sequence_step: int = 1,
        ) -> None:
            del session_id, token, glaas_url, sequence_start, sequence_step

        def append_fragment(self, fragment_dict: dict[str, object]) -> None:
            appended.append(fragment_dict)

        def flush(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setenv("ROAR_SESSION_ID", "session-bound-native")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "aa" * 32)
    monkeypatch.setenv("GLAAS_URL", "http://localhost:3001")
    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _FakeStreamer)
    monkeypatch.setattr(roar_worker, "_FLUSH_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(roar_worker, "_IDLE_FLUSH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(roar_worker, "_FLUSH_THRESHOLD_EVENTS", 200)
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-current")

    roar_worker._start_collector()
    with roar_worker._native_lock:
        roar_worker._native_events_buffer.append(
            (
                "task-launch",
                "write",
                ArtifactRef(
                    path="/tmp/bound-native-output.bin",
                    hash=None,
                    hash_algorithm="",
                    size=0,
                    capture_method="native",
                ),
            )
        )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not appended:
        time.sleep(0.01)

    roar_worker._shutdown_collector()

    assert appended, "collector did not flush bound native entries after going idle"
    fragment = appended[0]
    assert fragment["ray_task_id"] == "task-launch"
    assert fragment["writes"][0]["path"] == "/tmp/bound-native-output.bin"


def test_parse_and_buffer_frames_binds_registered_child_pid_to_launch_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msgpack

    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_should_track_local_path", lambda _path: True)
    roar_worker._register_native_child_pid(43210, "task-launch")

    payload = msgpack.packb(
        {
            "kind": "write",
            "pid": 43210,
            "path": "/tmp/child-native-output.bin",
        },
        use_bin_type=True,
    )
    buf = bytearray(len(payload).to_bytes(4, "little") + payload)

    roar_worker._parse_and_buffer_frames(buf)

    entries = roar_worker._drain_native_tracer_events()
    assert len(entries) == 1
    task_id, kind, ref = entries[0]
    assert task_id == "task-launch"
    assert kind == "write"
    assert ref.path == "/tmp/child-native-output.bin"


def test_parse_and_buffer_frames_binds_same_process_thread_to_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msgpack

    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_should_track_local_path", lambda _path: True)
    roar_worker._register_native_thread_task(54321, "task-thread")

    payload = msgpack.packb(
        {
            "kind": "write",
            "pid": roar_worker.os.getpid(),
            "thread_id": 54321,
            "path": "/tmp/thread-native-output.bin",
        },
        use_bin_type=True,
    )
    buf = bytearray(len(payload).to_bytes(4, "little") + payload)

    roar_worker._parse_and_buffer_frames(buf)

    entries = roar_worker._drain_native_tracer_events()
    assert len(entries) == 1
    task_id, kind, ref = entries[0]
    assert task_id == "task-thread"
    assert kind == "write"
    assert ref.path == "/tmp/thread-native-output.bin"


def test_wrap_task_executor_keeps_current_thread_bound_until_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-thread")
    monkeypatch.setattr(roar_worker.threading, "get_native_id", lambda: 321)
    monkeypatch.setattr(roar_worker, "_flush_current_task_native_events_immediately", lambda: None)

    observed: list[str] = []

    def _task() -> None:
        observed.append(
            roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 321)
        )

    wrapped = roar_worker._wrap_task_executor_for_native_flush(_task)
    wrapped()

    assert observed == ["task-thread"]
    assert roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 321) == "task-thread"

    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: "task-next")
    wrapped()
    assert roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 321) == "task-next"


def test_patch_threading_binds_spawned_thread_to_launching_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    observed: dict[str, str] = {}

    roar_worker._native_task_launch_context.task_id = "task-background"
    roar_worker._activate_threading_patch_for_native_task_attribution()

    def _target() -> None:
        thread_id = roar_worker.threading.get_native_id()
        observed["thread_id"] = str(thread_id)
        observed["task_id"] = roar_worker._bound_native_task_id_for_event(
            roar_worker.os.getpid(),
            thread_id,
        )

    thread = roar_worker.threading.Thread(target=_target, name="background")
    thread.start()
    thread.join(timeout=5)
    roar_worker._deactivate_threading_patch_for_native_task_attribution()

    assert not thread.is_alive()
    assert observed["task_id"] == "task-background"
    assert roar_worker._bound_native_task_id_for_event(
        roar_worker.os.getpid(),
        int(observed["thread_id"]),
    ) == "task-background"


def test_recent_thread_binding_expires_after_linger_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    now = {"value": 100.0}
    monkeypatch.setattr(roar_worker.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(roar_worker, "_RECENT_NATIVE_THREAD_BINDING_LINGER_SECONDS", 0.5)

    roar_worker._register_native_thread_task(4321, "task-thread")
    roar_worker._unregister_native_thread_task(4321, "task-thread")

    assert roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 4321) == "task-thread"

    now["value"] = 100.6
    assert roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 4321) == ""


def test_patch_wraps_actor_method_inside_executor_task_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    task_state = {"task_id": ""}
    monkeypatch.setattr(roar_worker, "_get_current_task_id", lambda: task_state["task_id"])
    monkeypatch.setattr(roar_worker.threading, "get_native_id", lambda: 321)
    monkeypatch.setattr(roar_worker, "_flush_current_task_native_events_immediately", lambda: None)

    function_execution_info = namedtuple(
        "FunctionExecutionInfo",
        ["function", "function_name", "max_calls"],
    )

    class _FakeFunctionActorManager:
        def __init__(self) -> None:
            self._function_execution_info: dict[str, object] = {}

        def get_execution_info(self, job_id, function_descriptor):
            del job_id, function_descriptor
            return function_execution_info(function=lambda: None, function_name="fn", max_calls=0)

        def _make_actor_method_executor(self, method_name, method):
            def executor(actor, *args, **kwargs):
                task_state["task_id"] = "task-actor"
                try:
                    return method(actor, *args, **kwargs)
                finally:
                    task_state["task_id"] = ""

            return executor

    fake_module = types.ModuleType("ray._private.function_manager")
    fake_module.FunctionActorManager = _FakeFunctionActorManager
    fake_module.FunctionExecutionInfo = function_execution_info
    monkeypatch.setitem(sys.modules, "ray._private.function_manager", fake_module)

    roar_worker._patch_ray_task_execution_for_native_flush()

    manager = _FakeFunctionActorManager()

    def _actor_method(_actor) -> str:
        return roar_worker._bound_native_task_id_for_event(roar_worker.os.getpid(), 321)

    executor = manager._make_actor_method_executor("write", _actor_method)
    assert executor(object()) == "task-actor"


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
    tmp_path: Path,
) -> None:
    import roar.ray.roar_worker as roar_worker

    captured: dict[str, object] = {}
    preload_library = tmp_path / "libroar_tracer_preload.so"
    preload_library.write_text("preload", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.delenv("ROAR_PRELOAD_TRACE_SOCK", raising=False)

    def _fake_execvp(program: str, argv: list[str]) -> None:
        captured["program"] = program
        captured["argv"] = argv
        captured["ld_preload"] = roar_worker.os.environ.get("LD_PRELOAD", "")
        captured["trace_sock"] = roar_worker.os.environ.get("ROAR_PRELOAD_TRACE_SOCK", "")
        raise SystemExit(0)

    monkeypatch.setattr(roar_worker.os, "execvp", _fake_execvp)

    with pytest.raises(SystemExit):
        roar_worker._run_worker_entrypoint(["-u", "worker.py"])

    assert captured["program"] == "python3"
    assert captured["argv"] == ["python3", "-u", "worker.py"]
    assert str(preload_library.resolve()) in str(captured["ld_preload"])
    assert str(captured["trace_sock"]).endswith("/trace.sock")


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
