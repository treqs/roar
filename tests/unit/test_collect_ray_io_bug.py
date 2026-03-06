from __future__ import annotations

import builtins
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

INJECT_DIR = Path(__file__).resolve().parents[2] / "roar" / "services" / "execution" / "inject"


def _load_sitecustomize_module():
    real_open = builtins.open
    real_import = builtins.__import__
    real_environ_get = os.environ.get

    spec = importlib.util.spec_from_file_location(
        "sitecustomize_collect_ray_io_bug_test",
        str(INJECT_DIR / "sitecustomize.py"),
    )
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.open = real_open
        builtins.__import__ = real_import
        os.environ.get = real_environ_get

    return module


class _AccessTrackingProxyLogs(dict[str, dict]):
    def __init__(self, payload: dict[str, dict]) -> None:
        super().__init__(payload)
        self.accessed = False

    def _mark(self) -> None:
        self.accessed = True

    def __getitem__(self, key):
        self._mark()
        return super().__getitem__(key)

    def __iter__(self):
        self._mark()
        return super().__iter__()

    def __len__(self) -> int:
        self._mark()
        return super().__len__()

    def get(self, key, default=None):
        self._mark()
        return super().get(key, default)

    def items(self):
        self._mark()
        return super().items()

    def keys(self):
        self._mark()
        return super().keys()

    def values(self):
        self._mark()
        return super().values()


def test_collect_ray_io_does_not_discard_proxy_logs_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sitecustomize = _load_sitecustomize_module()

    fake_ray = ModuleType("ray")
    fake_ray.is_initialized = lambda: True

    def _missing_actor(_name: str, namespace: str | None = None):
        del namespace
        raise ValueError("No actor")

    fake_ray.get_actor = _missing_actor
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")

    proxy_logs = _AccessTrackingProxyLogs(
        {
            "node-abc123": {
                "entries": [
                    "[S3:PutObject] s3://bucket/key.parquet  (1024 bytes)  etag=abc123",
                    "[S3:GetObject] s3://bucket/input.csv  (2048 bytes)  etag=def456",
                ],
                "node_id": "node-abc123",
                "proxy_port": 18080,
            }
        }
    )

    sitecustomize._collect_ray_io(proxy_logs=proxy_logs)

    # The fix should remove `del proxy_logs`, parse these proxy log entries into
    # artifact refs, and emit fragments directly even if the detached collector
    # actor has already been removed during the Phase 2 shutdown path.
    assert proxy_logs.accessed, (
        "_collect_ray_io returned without ever inspecting proxy_logs. "
        "This drops node-agent proxy log data on the floor when the collector actor is gone."
    )
