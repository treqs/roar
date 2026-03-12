from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from roar.backends.ray import runtime_hooks


@pytest.fixture(autouse=True)
def _reset_runtime_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROAR_EXECUTION_BACKEND", "ray")
    monkeypatch.setattr(runtime_hooks, "_ray_collect_pre_shutdown_registered", False)
    monkeypatch.setattr(runtime_hooks, "_runtime_process_initialized", False)
    monkeypatch.setattr(runtime_hooks, "_driver_phase_subprocess_patched", False)
    monkeypatch.setattr(runtime_hooks, "_driver_phase_s3_clients_patched", False)
    monkeypatch.setattr(runtime_hooks, "_ray_node_poller_thread", None)
    monkeypatch.setattr(runtime_hooks, "_ray_node_poller_stop", threading.Event())
    monkeypatch.setattr(runtime_hooks, "_ray_node_agents", {})


def test_patch_ray_init_skips_pip_dependency_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ROAR_JOB_ID", raising=False)

    import importlib.metadata as importlib_metadata

    monkeypatch.setattr(importlib_metadata, "version", lambda _: "9.8.7")

    runtime_hooks.patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    assert result == "ok"
    runtime_env = calls[-1]["runtime_env"]
    assert "pip" not in runtime_env
    assert {"USER_KEY", "ROAR_JOB_ID", "ROAR_DRIVER_JOB_UID"}.issubset(runtime_env["env_vars"])
    assert runtime_env["env_vars"]["ROAR_JOB_ID"]


def test_patch_ray_init_skips_injection_when_ray_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    config_path = tmp_path / ".roar" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
[ray]
enabled = false
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    runtime_hooks.patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    assert calls[-1]["runtime_env"] == {"env_vars": {"USER_KEY": "value"}}


def test_patch_ray_init_honors_ray_config_pip_toggle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    config_path = tmp_path / ".roar" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
[ray]
pip_install = false
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    runtime_hooks.patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={})

    assert "pip" not in calls[-1]["runtime_env"]


def test_patch_ray_shutdown_collects_before_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    call_order: list[str] = []

    def fake_shutdown(*_args, **_kwargs):
        call_order.append("shutdown")

    fake_ray = SimpleNamespace(shutdown=fake_shutdown)
    monkeypatch.setattr(
        runtime_hooks,
        "collect_ray_io",
        lambda *args, **kwargs: call_order.append("collect"),
    )

    runtime_hooks.patch_ray_shutdown(fake_ray)
    fake_ray.shutdown()

    assert call_order == ["collect", "shutdown"]


def test_shutdown_ray_at_exit_prefers_ray_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    call_order: list[str] = []
    fake_ray = SimpleNamespace(
        is_initialized=lambda: True,
        shutdown=lambda: call_order.append("shutdown"),
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr(
        runtime_hooks,
        "collect_ray_io",
        lambda *args, **kwargs: call_order.append("collect"),
    )

    runtime_hooks.shutdown_ray_at_exit()

    assert call_order == ["shutdown"]


def test_patch_ray_init_starts_node_agent_spawn_in_background_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setenv("ROAR_RAY_NODE_AGENTS", "1")
    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    monkeypatch.setattr(runtime_hooks, "start_ray_node_poller", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime_hooks,
        "spawn_node_agents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    thread_targets: list[tuple[object, tuple, dict, str | None, bool | None]] = []

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, name=None, daemon=None):
            thread_targets.append((target, args, kwargs or {}, name, daemon))

        def start(self):
            return None

    monkeypatch.setattr(runtime_hooks.threading, "Thread", _FakeThread)

    runtime_hooks.patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    assert thread_targets
    assert thread_targets[0][0] is runtime_hooks.spawn_node_agents


def test_patch_ray_init_skips_node_agent_spawn_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    spawn_node_agents = MagicMock()
    start_node_poller = MagicMock()
    monkeypatch.setattr(runtime_hooks, "spawn_node_agents", spawn_node_agents)
    monkeypatch.setattr(runtime_hooks, "start_ray_node_poller", start_node_poller)

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, name=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(runtime_hooks.threading, "Thread", _FakeThread)

    runtime_hooks.patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    spawn_node_agents.assert_not_called()
    start_node_poller.assert_not_called()


def test_prepare_worker_runtime_env_sets_roar_worker_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = runtime_hooks.prepare_worker_runtime_env({}, "jobworker")

    assert prepared["py_executable"] == "roar-worker"
    assert (
        prepared["worker_process_setup_hook"] == "roar.services.execution.worker_bootstrap.startup"
    )
    assert (
        prepared["env_vars"][runtime_hooks._WORKER_SETUP_HOOK_ENV_VAR]
        == "roar.services.execution.worker_bootstrap.startup"
    )


def test_prepare_worker_runtime_env_ignores_existing_worker_setup_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = runtime_hooks.prepare_worker_runtime_env(
        {"worker_process_setup_hook": "backend.custom.setup"},
        "job1111",
    )

    assert prepared["py_executable"] == "roar-worker"
    assert (
        prepared["worker_process_setup_hook"] == "roar.services.execution.worker_bootstrap.startup"
    )


def test_patch_ray_init_propagates_fragment_streaming_env_to_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    monkeypatch.setenv("ROAR_CLUSTER_GLAAS_URL", "http://host.docker.internal:3001")
    monkeypatch.setenv("ROAR_SESSION_ID", "session-123")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ab" * 32)
    monkeypatch.setenv("ROAR_JOB_ID", "driver-uid-1234")

    runtime_hooks.patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={})

    runtime_env = calls[-1]["runtime_env"]
    assert runtime_env["env_vars"]["GLAAS_URL"] == "http://host.docker.internal:3001"
    assert runtime_env["env_vars"]["ROAR_SESSION_ID"] == "session-123"
    assert runtime_env["env_vars"]["ROAR_FRAGMENT_TOKEN"] == "ab" * 32


def test_prepare_worker_env_vars_preserves_existing_cluster_visible_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROAR_CLUSTER_GLAAS_URL", "http://host.docker.internal:3001")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:24567")
    monkeypatch.setenv("ROAR_PROXY_PORT", "24567")
    monkeypatch.setenv("ROAR_UPSTREAM_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("ROAR_SESSION_ID", "session-123")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ab" * 32)

    env_vars = runtime_hooks.prepare_worker_env_vars(
        {
            "GLAAS_URL": "http://user-configured:3001",
            "AWS_ENDPOINT_URL": "http://user-endpoint:9000",
        },
        job_id="job-worker-1",
        driver_job_uid="driver-1234",
    )

    assert env_vars["ROAR_JOB_ID"] == "job-worker-1"
    assert env_vars["ROAR_DRIVER_JOB_UID"] == "driver-1234"
    assert env_vars["GLAAS_URL"] == "http://user-configured:3001"
    assert env_vars["AWS_ENDPOINT_URL"] == "http://user-endpoint:9000"
    assert env_vars["ROAR_PROXY_PORT"] == "24567"
    assert env_vars["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://minio:9000"
    assert env_vars["ROAR_SESSION_ID"] == "session-123"
    assert env_vars["ROAR_FRAGMENT_TOKEN"] == "ab" * 32


def test_patch_ray_init_sets_driver_job_uid_for_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    monkeypatch.setenv("ROAR_JOB_ID", "driver-uid-1234")

    runtime_hooks.patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={})

    runtime_env = calls[-1]["runtime_env"]
    assert runtime_env["py_executable"] == "roar-worker"
    assert (
        runtime_env["worker_process_setup_hook"]
        == "roar.services.execution.worker_bootstrap.startup"
    )
    assert runtime_env["env_vars"]["ROAR_DRIVER_JOB_UID"] == "driver-uid-1234"


def test_patch_ray_init_conflicts_inside_preinstrumented_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_runtime_env = {
        "pip": ["roar-cli==0.0.1"],
        "working_dir": "/tmp/job-level-working-dir",
        "env_vars": {"USER_KEY": "value"},
    }
    captured_runtime_env: dict[str, object] = {}

    def fake_ray_init(*_args, **kwargs):
        runtime_env = dict(kwargs.get("runtime_env", {}) or {})
        captured_runtime_env.update(runtime_env)
        conflicting_keys = {
            key
            for key in ("pip", "working_dir")
            if key in runtime_env
            and key in job_runtime_env
            and runtime_env[key] != job_runtime_env[key]
        }
        if conflicting_keys:
            raise AssertionError(f"unexpected conflicts: {sorted(conflicting_keys)}")
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)

    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": True},
    )
    monkeypatch.setattr(
        runtime_hooks,
        "merge_roar_runtime_env_pip",
        lambda _existing: ["roar-cli==9.9.9"],
    )
    monkeypatch.setattr(
        runtime_hooks,
        "prepare_worker_runtime_env",
        lambda _runtime_env, _job_id: (_ for _ in ()).throw(
            AssertionError("prepare_worker_runtime_env should be skipped in instrumented jobs")
        ),
    )
    monkeypatch.setattr(
        runtime_hooks,
        "sanitize_worker_runtime_env_for_ray",
        lambda _ray_module, runtime_env: runtime_env,
    )
    monkeypatch.setattr(
        runtime_hooks,
        "register_pre_shutdown_ray_collection",
        lambda: None,
    )
    monkeypatch.setenv("ROAR_JOB_INSTRUMENTED", "1")

    runtime_hooks.patch_ray_init(fake_ray)

    result = fake_ray.init(runtime_env=job_runtime_env)
    assert result == "ok"
    assert captured_runtime_env["pip"] == ["roar-cli==0.0.1"]
    assert captured_runtime_env["working_dir"] == "/tmp/job-level-working-dir"
    assert captured_runtime_env["py_executable"] == "roar-worker"
    assert captured_runtime_env["env_vars"]["USER_KEY"] == "value"


def test_patch_ray_init_registers_pre_shutdown_collection_for_instrumented_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = SimpleNamespace(init=lambda *_args, **_kwargs: "ok")
    register_calls: list[str] = []

    monkeypatch.setattr(
        runtime_hooks,
        "load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    monkeypatch.setattr(
        runtime_hooks,
        "register_pre_shutdown_ray_collection",
        lambda: register_calls.append("called"),
    )
    monkeypatch.setenv("ROAR_JOB_INSTRUMENTED", "1")
    monkeypatch.setenv("ROAR_RAY_NODE_AGENTS", "0")

    runtime_hooks.patch_ray_init(fake_ray)

    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    assert register_calls == ["called"]


def test_initialize_runtime_process_patches_driver_phase_subprocess_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ROAR_DRIVER_PHASE_CAPTURE", "1")
    monkeypatch.setattr(
        runtime_hooks,
        "patch_driver_phase_subprocess_capture",
        lambda: calls.append("subprocess"),
    )

    runtime_hooks.initialize_runtime_process()
    runtime_hooks.initialize_runtime_process()

    assert calls == ["subprocess"]


def test_observe_runtime_import_patches_driver_phase_clients_for_boto3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ROAR_DRIVER_PHASE_PROXY_URL", "http://127.0.0.1:19191")
    monkeypatch.setattr(
        runtime_hooks,
        "patch_driver_phase_s3_clients",
        lambda: calls.append("patch"),
    )

    runtime_hooks.observe_runtime_import("boto3", object())
    runtime_hooks.observe_runtime_import("boto3.session", object())

    assert calls == ["patch"]


def test_collect_ray_io_does_not_discard_proxy_logs_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = ModuleType("ray")
    fake_ray.is_initialized = lambda: True

    def _missing_actor(_name: str, namespace: str | None = None):
        del namespace
        raise ValueError("No actor")

    fake_ray.get_actor = _missing_actor
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")

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

    runtime_hooks.collect_ray_io(proxy_logs=proxy_logs)

    assert proxy_logs.accessed


def test_collect_ray_io_collects_node_agent_logs_when_called_without_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ray = ModuleType("ray")
    fake_ray.is_initialized = lambda: True
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv("ROAR_RAY_NODE_AGENTS", "1")

    parsed_refs = []
    monkeypatch.setattr(
        runtime_hooks,
        "collect_node_agent_logs",
        lambda _ray_module: {
            "node-abc123": {
                "proxy_log_lines": [
                    "[S3:PutObject] s3://bucket/key.parquet  (1024 bytes)  etag=abc123",
                    "[S3:GetObject] s3://bucket/input.csv  (2048 bytes)  etag=def456",
                ],
                "node_id": "node-abc123",
                "proxy_port": 18080,
            }
        },
    )
    monkeypatch.setattr(
        runtime_hooks,
        "_write_proxy_artifacts_to_db",
        lambda refs: parsed_refs.extend(refs),
    )

    runtime_hooks.collect_ray_io()

    assert [kind for kind, _ref in parsed_refs] == ["write", "read"]
