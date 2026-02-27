from __future__ import annotations

import builtins
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from roar.ray import worker


def test_setup_defers_actor_initialization_until_event_logging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ROAR_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "_choose_backend", lambda: "actor")
    init_actor = MagicMock()
    monkeypatch.setattr(worker, "_init_actor", init_actor)
    monkeypatch.setattr(worker, "_patch_boto3", lambda: None)
    monkeypatch.setattr(worker, "_patch_pandas", lambda: None)
    monkeypatch.setattr(worker, "_patch_pyarrow_filesystem", lambda: None)
    monkeypatch.setattr(worker, "_patch_ray_data", lambda: None)
    monkeypatch.setattr(worker, "_configure_local_proxy_endpoint", lambda: None)
    monkeypatch.setattr(worker.atexit, "register", lambda *_args, **_kwargs: None)

    if hasattr(worker.setup, "_roar_worker_ready"):
        delattr(worker.setup, "_roar_worker_ready")

    original_open = builtins.open
    try:
        worker.setup()
    finally:
        builtins.open = original_open
        if hasattr(worker.setup, "_roar_worker_ready"):
            delattr(worker.setup, "_roar_worker_ready")

    init_actor.assert_not_called()


def test_setup_skips_proxy_endpoint_configuration_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ROAR_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "_choose_backend", lambda: "filesystem")
    monkeypatch.setattr(worker, "_patch_boto3", lambda: None)
    monkeypatch.setattr(worker, "_patch_pandas", lambda: None)
    monkeypatch.setattr(worker, "_patch_pyarrow_filesystem", lambda: None)
    monkeypatch.setattr(worker, "_patch_ray_data", lambda: None)
    configure_proxy = MagicMock()
    monkeypatch.setattr(worker, "_configure_local_proxy_endpoint", configure_proxy)
    monkeypatch.setattr(worker.atexit, "register", lambda *_args, **_kwargs: None)

    if hasattr(worker.setup, "_roar_worker_ready"):
        delattr(worker.setup, "_roar_worker_ready")

    original_open = builtins.open
    try:
        worker.setup()
    finally:
        builtins.open = original_open
        if hasattr(worker.setup, "_roar_worker_ready"):
            delattr(worker.setup, "_roar_worker_ready")

    configure_proxy.assert_not_called()


def test_setup_skips_optional_sdk_patches_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ROAR_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "_choose_backend", lambda: "filesystem")
    patch_boto3 = MagicMock()
    patch_pandas = MagicMock()
    patch_pyarrow = MagicMock()
    patch_ray_data = MagicMock()
    monkeypatch.setattr(worker, "_patch_boto3", patch_boto3)
    monkeypatch.setattr(worker, "_patch_pandas", patch_pandas)
    monkeypatch.setattr(worker, "_patch_pyarrow_filesystem", patch_pyarrow)
    monkeypatch.setattr(worker, "_patch_ray_data", patch_ray_data)
    monkeypatch.setattr(worker, "_configure_local_proxy_endpoint", lambda: None)
    monkeypatch.setattr(worker.atexit, "register", lambda *_args, **_kwargs: None)

    if hasattr(worker.setup, "_roar_worker_ready"):
        delattr(worker.setup, "_roar_worker_ready")

    original_open = builtins.open
    try:
        worker.setup()
    finally:
        builtins.open = original_open
        if hasattr(worker.setup, "_roar_worker_ready"):
            delattr(worker.setup, "_roar_worker_ready")

    patch_boto3.assert_not_called()
    patch_pandas.assert_not_called()
    patch_pyarrow.assert_not_called()
    patch_ray_data.assert_not_called()


def test_init_actor_only_attempts_lookup_when_actor_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRay:
        def __init__(self) -> None:
            self.get_actor_calls: list[tuple[str, str | None]] = []

        def get_actor(self, name: str, namespace: str | None = None):
            self.get_actor_calls.append((name, namespace))
            raise ValueError("missing")

    class _FakeCollectorActor:
        options_called = False

        @classmethod
        def options(cls, **_kwargs):
            cls.options_called = True
            return SimpleNamespace(remote=lambda: "ok")

    monkeypatch.setitem(sys.modules, "ray", _FakeRay())
    monkeypatch.setitem(
        sys.modules,
        "roar.ray.actor",
        SimpleNamespace(RoarLogCollectorActor=_FakeCollectorActor),
    )
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")
    monkeypatch.setattr(worker, "_actor", None)

    worker._init_actor()

    assert _FakeCollectorActor.options_called is False


def test_runtime_context_ids_short_circuit_when_ray_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRay:
        context_lookups = 0

        def is_initialized(self) -> bool:
            return False

        def get_runtime_context(self):
            self.context_lookups += 1
            return SimpleNamespace(get_task_id=lambda: None, get_node_id=lambda: None)

    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    task_id, node_id = worker._runtime_context_ids()

    assert task_id is None
    assert node_id is None
    assert fake_ray.context_lookups == 0


def test_patch_tempfile_logs_named_temporary_file_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(worker, "_runtime_context_ids", lambda: ("task-123", None))
    monkeypatch.setattr(worker, "_BACKEND", "filesystem")
    monkeypatch.setattr(worker, "_actor", None)
    monkeypatch.setattr(worker, "_LOG_DIR", str(tmp_path))

    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        worker,
        "_write_to_file",
        lambda task_id, payload: captured.append((task_id, payload)),
    )

    original_named_tempfile = tempfile.NamedTemporaryFile
    try:
        worker._patch_tempfile()
        with tempfile.NamedTemporaryFile(delete=True, dir=tmp_path):
            pass
    finally:
        tempfile.NamedTemporaryFile = original_named_tempfile
        if hasattr(tempfile, "_roar_worker_tempfile_patched"):
            delattr(tempfile, "_roar_worker_tempfile_patched")

    assert captured
    task_id, payload = captured[0]
    assert task_id == "task-123"
    assert payload["mode"] == "w"
    assert str(payload["path"]).startswith(str(tmp_path))


def test_log_access_flushes_actor_buffer_for_small_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: list[list[dict[str, object]]] = []

    class _FakeAppendBatch:
        @staticmethod
        def remote(batch):
            batches.append(list(batch))

    class _FakeActor:
        append_batch = _FakeAppendBatch()

    monkeypatch.setattr(worker, "_BACKEND", "actor")
    monkeypatch.setattr(worker, "_actor", _FakeActor())
    monkeypatch.setattr(worker, "_event_buffer", [])
    monkeypatch.setattr(worker, "_runtime_context_ids", lambda: ("task-abc", None))
    monkeypatch.setattr(worker, "_init_actor", lambda: None)
    monkeypatch.setattr(worker, "_FLUSH_THRESHOLD", 50)

    worker._log_access("/tmp/demo.txt", "w", capture_method="python")

    assert batches
    assert batches[0][0]["path"] == "/tmp/demo.txt"
