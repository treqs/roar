from __future__ import annotations

import builtins
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
    monkeypatch.setattr(sitecustomize.importlib_metadata, "version", lambda _: "9.8.7")

    sitecustomize._patch_ray_init(fake_ray)
    result = fake_ray.init(runtime_env={"env_vars": {"USER_KEY": "value"}})

    assert result == "ok"
    runtime_env = calls[-1]["runtime_env"]
    assert runtime_env["pip"] == ["roar-cli==9.8.7"]


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
