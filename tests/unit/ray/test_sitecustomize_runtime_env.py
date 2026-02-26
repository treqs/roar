from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from roar.services.execution.inject import sitecustomize


@pytest.fixture(autouse=True)
def _restore_builtins():
    real_open = builtins.open
    real_import = builtins.__import__
    try:
        yield
    finally:
        builtins.open = real_open
        builtins.__import__ = real_import


def test_patch_ray_init_skips_pip_dependency_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("ROAR_LOG_DIR", "/tmp/roar-ray")
    monkeypatch.delenv("ROAR_JOB_ID", raising=False)
    monkeypatch.setattr(sitecustomize.importlib_metadata, "version", lambda _: "9.8.7")

    sitecustomize._patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    assert result == "ok"
    runtime_env = calls[-1]["runtime_env"]
    assert "pip" not in runtime_env
    assert runtime_env["env_vars"]["ROAR_LOG_BACKEND"] == "actor"
    assert runtime_env["env_vars"]["ROAR_JOB_ID"]


def test_patch_ray_init_skips_injection_when_ray_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    config_path = tmp_path / ".roar" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("""
[ray]
enabled = false
""")
    monkeypatch.chdir(tmp_path)

    sitecustomize._patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    runtime_env = calls[-1]["runtime_env"]
    assert runtime_env == {"env_vars": {"USER_KEY": "value"}}


def test_patch_ray_init_honors_ray_config_log_dir_and_pip_toggle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    config_path = tmp_path / ".roar" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("""
[ray]
pip_install = false
log_dir = "/tmp/roar-ray-config"
""")
    monkeypatch.chdir(tmp_path)

    sitecustomize._patch_ray_init(fake_ray)
    fake_ray.init(runtime_env={})

    runtime_env = calls[-1]["runtime_env"]
    assert "pip" not in runtime_env
    assert runtime_env["env_vars"]["ROAR_LOG_DIR"] == "/tmp/roar-ray-config"


def test_patch_ray_shutdown_collects_before_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    call_order: list[str] = []

    def fake_shutdown(*_args, **_kwargs):
        call_order.append("shutdown")

    fake_ray = SimpleNamespace(shutdown=fake_shutdown)
    monkeypatch.setattr(
        sitecustomize,
        "_collect_ray_io",
        lambda *args, **kwargs: call_order.append("collect"),
    )

    sitecustomize._patch_ray_shutdown(fake_ray)
    fake_ray.shutdown()

    assert call_order == ["collect", "shutdown"]


def test_ensure_collector_actor_creates_named_actor_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRay:
        def __init__(self) -> None:
            self.get_actor_calls: list[tuple[str, str | None]] = []
            self.get_calls: list[tuple[object, int | None]] = []

        def get_actor(self, name: str, namespace: str | None = None):
            self.get_actor_calls.append((name, namespace))
            raise ValueError("missing")

        def get(self, value, timeout: int | None = None):
            self.get_calls.append((value, timeout))
            return value

    created: dict[str, object] = {}

    class _FakeCollectorActor:
        class _FakeRemoteMethod:
            @staticmethod
            def remote():
                return "ready"

        @classmethod
        def options(cls, **kwargs):
            created["options"] = kwargs
            return SimpleNamespace(
                remote=lambda: SimpleNamespace(get_all=cls._FakeRemoteMethod()),
            )

    fake_actor_module = ModuleType("roar.ray.actor")
    fake_actor_module.RoarLogCollectorActor = _FakeCollectorActor
    monkeypatch.setitem(
        sys.modules,
        "roar.ray.actor",
        fake_actor_module,
    )

    fake_ray = _FakeRay()
    sitecustomize._ensure_collector_actor(fake_ray, "job1234")

    assert fake_ray.get_actor_calls == [("roar-log-collector-job1234", "roar")]
    assert created["options"] == {
        "name": "roar-log-collector-job1234",
        "namespace": "roar",
        "lifetime": "detached",
        "num_cpus": 0,
    }
    assert fake_ray.get_calls == [("ready", 10)]


def test_patch_ray_init_starts_node_agent_spawn_in_background_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ROAR_RAY_NODE_AGENTS", "1")
    monkeypatch.setattr(
        sitecustomize,
        "_load_ray_config",
        lambda: {"enabled": True, "pip_install": False, "log_dir": "/tmp/roar-ray"},
    )
    monkeypatch.setattr(sitecustomize, "_start_ray_node_poller", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sitecustomize,
        "_spawn_node_agents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    thread_targets: list[tuple[object, tuple, dict, str | None, bool | None]] = []

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, name=None, daemon=None):
            thread_targets.append((target, args, kwargs or {}, name, daemon))

        def start(self):
            return None

    monkeypatch.setattr(sitecustomize.threading, "Thread", _FakeThread)

    sitecustomize._patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    assert thread_targets
    assert thread_targets[0][0] is sitecustomize._spawn_node_agents


def test_patch_ray_init_skips_node_agent_spawn_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sitecustomize,
        "_load_ray_config",
        lambda: {"enabled": True, "pip_install": False, "log_dir": "/tmp/roar-ray"},
    )
    spawn_node_agents = MagicMock()
    start_node_poller = MagicMock()
    monkeypatch.setattr(sitecustomize, "_spawn_node_agents", spawn_node_agents)
    monkeypatch.setattr(sitecustomize, "_start_ray_node_poller", start_node_poller)

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, name=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(sitecustomize.threading, "Thread", _FakeThread)

    sitecustomize._patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    spawn_node_agents.assert_not_called()
    start_node_poller.assert_not_called()


def test_prepare_worker_runtime_env_sets_wrapper_and_preload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_working_dir = tmp_path / "user-working-dir"
    source_working_dir.mkdir()
    (source_working_dir / "user-file.txt").write_text("hello", encoding="utf-8")

    preload_path = tmp_path / "libroar_tracer_preload.so"
    preload_path.write_text("preload", encoding="utf-8")
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: str(preload_path),
    )

    prepared = sitecustomize._prepare_worker_runtime_env(
        {"working_dir": str(source_working_dir)},
        "job1234",
    )

    working_dir = Path(str(prepared["working_dir"]))
    assert prepared["py_executable"] == "bash ./roar_worker_wrapper.sh"
    assert (working_dir / "user-file.txt").read_text(encoding="utf-8") == "hello"
    assert (working_dir / "libroar_tracer_preload.so").read_text(encoding="utf-8") == "preload"

    wrapper = (working_dir / "roar_worker_wrapper.sh").read_text(encoding="utf-8")
    assert "export LD_PRELOAD" in wrapper
    assert "exec python3 \"$@\"" in wrapper


def test_prepare_worker_runtime_env_warns_when_working_dir_is_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        sitecustomize,
        "_warn_roar",
        lambda message, *args: warnings.append(message % args if args else message),
    )
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = sitecustomize._prepare_worker_runtime_env(
        {"working_dir": "s3://bucket/path"},
        "job5678",
    )

    assert warnings
    assert prepared["py_executable"] == "bash ./roar_worker_wrapper.sh"
    assert Path(str(prepared["working_dir"])).exists()


def test_prepare_worker_runtime_env_bundles_roar_worker_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = sitecustomize._prepare_worker_runtime_env({}, "job9999")
    working_dir = Path(str(prepared["working_dir"]))

    assert (working_dir / "roar" / "ray" / "worker.py").exists()


def test_prepare_worker_runtime_env_writes_worker_sitecustomize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "roar.services.execution.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = sitecustomize._prepare_worker_runtime_env({}, "job3210")
    working_dir = Path(str(prepared["working_dir"]))
    worker_sitecustomize = (working_dir / "sitecustomize.py").read_text(encoding="utf-8")

    assert "ROAR_WORKER" in worker_sitecustomize
    assert "default_worker.py" in worker_sitecustomize
    assert "roar.ray.worker" in worker_sitecustomize
