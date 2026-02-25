from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

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


def test_patch_ray_init_injects_roar_pip_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)

    monkeypatch.setenv("ROAR_LOG_DIR", "/tmp/roar-ray")
    monkeypatch.delenv("ROAR_JOB_ID", raising=False)
    monkeypatch.setattr(sitecustomize.importlib_metadata, "version", lambda _: "9.8.7")

    sitecustomize._patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    assert result == "ok"
    runtime_env = calls[-1]["runtime_env"]
    assert runtime_env["pip"] == ["roar-cli==9.8.7"]
    assert runtime_env["env_vars"]["ROAR_JOB_ID"]


def test_patch_ray_init_skips_injection_when_ray_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[dict] = []

    def fake_ray_init(*_args, **kwargs):
        calls.append(kwargs)
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)
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
