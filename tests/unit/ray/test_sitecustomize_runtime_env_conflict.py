from __future__ import annotations

from types import SimpleNamespace

import pytest

from roar.services.execution.inject import sitecustomize


def test_patch_ray_init_conflicts_inside_preinstrumented_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_runtime_env = {
        "pip": ["roar-cli==0.0.1"],
        "working_dir": "/tmp/job-level-working-dir",
        "env_vars": {"ROAR_WORKER": "1"},
    }

    def fake_prepare_worker_runtime_env(_runtime_env: dict, _job_id: str) -> dict:
        raise AssertionError("_prepare_worker_runtime_env should be skipped in instrumented jobs")

    def fake_ray_init(*_args, **kwargs):
        runtime_env = dict(kwargs.get("runtime_env", {}) or {})
        conflicting_keys = {
            key
            for key in ("pip", "working_dir")
            if key in runtime_env and key in job_runtime_env and runtime_env[key] != job_runtime_env[key]
        }
        if conflicting_keys:
            assert conflicting_keys == {"pip", "working_dir"}
            raise ValueError(
                "Failed to merge the Job's runtime env because of a conflict "
                "on pip and working_dir"
            )
        return "ok"

    fake_ray = SimpleNamespace(init=fake_ray_init)

    monkeypatch.setattr(
        sitecustomize,
        "_load_ray_config",
        lambda: {"enabled": True, "pip_install": True, "log_dir": "/tmp/roar-ray"},
    )
    monkeypatch.setattr(
        sitecustomize,
        "_merge_roar_runtime_env_pip",
        lambda _existing: ["roar-cli==9.9.9"],
    )
    monkeypatch.setattr(sitecustomize, "_prepare_worker_runtime_env", fake_prepare_worker_runtime_env)
    monkeypatch.setattr(
        sitecustomize,
        "_sanitize_worker_runtime_env_for_ray",
        lambda _ray_module, runtime_env: runtime_env,
    )
    monkeypatch.setattr(sitecustomize, "_register_pre_shutdown_ray_collection", lambda: None)
    monkeypatch.setattr(sitecustomize, "_ensure_collector_actor", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ROAR_JOB_INSTRUMENTED", "1")

    sitecustomize._patch_ray_init(fake_ray)

    result = fake_ray.init(runtime_env=job_runtime_env)
    assert result == "ok"
