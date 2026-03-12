from __future__ import annotations

from types import SimpleNamespace

import pytest

from roar.services.execution.inject import sitecustomize


@pytest.fixture(autouse=True)
def _set_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROAR_EXECUTION_BACKEND", "ray")


def test_patch_ray_init_conflicts_inside_preinstrumented_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_runtime_env = {
        "pip": ["roar-cli==0.0.1"],
        "working_dir": "/tmp/job-level-working-dir",
        "env_vars": {"USER_KEY": "value"},
    }
    captured_runtime_env: dict[str, object] = {}

    def fake_prepare_worker_runtime_env(_runtime_env: dict, _job_id: str) -> dict:
        raise AssertionError("_prepare_worker_runtime_env should be skipped in instrumented jobs")

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
            assert conflicting_keys == {"pip", "working_dir"}
            raise ValueError(
                "Failed to merge the Job's runtime env because of a conflict on pip and working_dir"
            )
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)

    monkeypatch.setattr(
        sitecustomize,
        "_load_ray_config",
        lambda: {"enabled": True, "pip_install": True},
    )
    monkeypatch.setattr(
        sitecustomize,
        "_merge_roar_runtime_env_pip",
        lambda _existing: ["roar-cli==9.9.9"],
    )
    monkeypatch.setattr(
        sitecustomize, "_prepare_worker_runtime_env", fake_prepare_worker_runtime_env
    )
    monkeypatch.setattr(
        sitecustomize,
        "_sanitize_worker_runtime_env_for_ray",
        lambda _ray_module, runtime_env: runtime_env,
    )
    monkeypatch.setattr(sitecustomize, "_register_pre_shutdown_ray_collection", lambda: None)
    monkeypatch.setenv("ROAR_JOB_INSTRUMENTED", "1")

    sitecustomize._patch_ray_init(fake_ray)

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
        sitecustomize,
        "_load_ray_config",
        lambda: {"enabled": True, "pip_install": False},
    )
    monkeypatch.setattr(
        sitecustomize,
        "_register_pre_shutdown_ray_collection",
        lambda: register_calls.append("called"),
    )
    monkeypatch.setenv("ROAR_JOB_INSTRUMENTED", "1")
    monkeypatch.setenv("ROAR_RAY_NODE_AGENTS", "0")

    sitecustomize._patch_ray_init(fake_ray)

    result = fake_ray.init(runtime_env={})

    assert result == "ok"
    assert register_calls == ["called"]
