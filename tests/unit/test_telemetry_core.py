from __future__ import annotations

import json
from pathlib import Path

from roar import telemetry
from roar.telemetry import config, paths, queue, stats
from roar.telemetry.capabilities import DEFAULT_TELEMETRY_ENDPOINT


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
    }


def test_xdg_aware_paths_resolve_config_cache_stats_queue_and_lock(tmp_path: Path) -> None:
    env = _env(tmp_path)

    resolved = paths.resolve_paths(env)

    assert resolved.global_config_file == tmp_path / "xdg-config" / "roar" / "config.toml"
    assert resolved.cache_dir == tmp_path / "xdg-cache" / "roar"
    assert resolved.stats_file == tmp_path / "xdg-cache" / "roar" / "stats.json"
    assert resolved.queue_dir == tmp_path / "xdg-cache" / "roar" / "telemetry" / "queue"
    assert resolved.lock_file == tmp_path / "xdg-cache" / "roar" / "telemetry" / "telemetry.lock"


def test_config_resolver_env_global_project_default_precedence(tmp_path: Path) -> None:
    env = _env(tmp_path)
    resolved_paths = paths.resolve_paths(env)
    project = tmp_path / "project"
    project_roar = project / ".roar"
    project_roar.mkdir(parents=True)
    (project_roar / "config.toml").write_text(
        """
[telemetry]
enabled = false
endpoint = "https://project.example/telemetry"
""".strip(),
        encoding="utf-8",
    )

    effective = config.resolve_config(start_dir=project, environ=env, paths=resolved_paths)
    assert not effective.stats_enabled
    assert effective.disabled_reason == "project_disabled"
    assert effective.endpoint == "https://project.example/telemetry"
    assert effective.endpoint_source == "project"

    resolved_paths.global_config_file.parent.mkdir(parents=True)
    resolved_paths.global_config_file.write_text(
        """
[telemetry]
enabled = false
endpoint = "https://global.example/telemetry"
""".strip(),
        encoding="utf-8",
    )

    effective = config.resolve_config(start_dir=project, environ=env, paths=resolved_paths)
    assert not effective.stats_enabled
    assert effective.disabled_reason == "global_disabled"
    assert effective.endpoint == "https://global.example/telemetry"
    assert effective.endpoint_source == "global"

    effective = config.resolve_config(
        start_dir=project,
        environ={**env, "ROAR_TELEMETRY_ENDPOINT": "https://env.example/telemetry"},
        paths=resolved_paths,
    )
    assert effective.endpoint == "https://env.example/telemetry"
    assert effective.endpoint_source == "env"

    effective = config.resolve_config(
        start_dir=project,
        environ={**env, "ROAR_NO_TELEMETRY": "1"},
        paths=resolved_paths,
    )
    assert not effective.stats_enabled
    assert effective.suppression_reason == "roar_no_telemetry"


def test_default_config_is_enabled_with_default_endpoint(tmp_path: Path) -> None:
    env = _env(tmp_path)
    effective = config.resolve_config(environ=env, paths=paths.resolve_paths(env))

    assert effective.stats_enabled
    assert effective.uploads_enabled
    assert effective.endpoint == DEFAULT_TELEMETRY_ENDPOINT
    assert effective.endpoint_source == "default"


def test_config_resolver_derives_endpoint_from_project_glaas_url(tmp_path: Path) -> None:
    env = _env(tmp_path)
    project = tmp_path / "project"
    project_roar = project / ".roar"
    project_roar.mkdir(parents=True)
    (project_roar / "config.toml").write_text(
        """
[glaas]
url = "https://api.dev.glaas.ai/"

[telemetry]
enabled = true
""".strip(),
        encoding="utf-8",
    )

    effective = config.resolve_config(
        start_dir=project, environ=env, paths=paths.resolve_paths(env)
    )

    assert effective.endpoint == "https://api.dev.glaas.ai/api/v1/telemetry/roar"
    assert effective.endpoint_source == "glaas"


def test_config_resolver_explicit_telemetry_endpoint_overrides_derived_glaas_url(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    project = tmp_path / "project"
    project_roar = project / ".roar"
    project_roar.mkdir(parents=True)
    (project_roar / "config.toml").write_text(
        """
[glaas]
url = "https://api.dev.glaas.ai"

[telemetry]
endpoint = "https://collector.example/roar"
""".strip(),
        encoding="utf-8",
    )

    effective = config.resolve_config(
        start_dir=project, environ=env, paths=paths.resolve_paths(env)
    )

    assert effective.endpoint == "https://collector.example/roar"
    assert effective.endpoint_source == "project"


def test_suppression_covers_opt_out_ci_and_backend_environments(tmp_path: Path) -> None:
    base_env = _env(tmp_path)
    cases = [
        ("DO_NOT_TRACK", "1", "do_not_track"),
        ("ROAR_NO_TELEMETRY", "1", "roar_no_telemetry"),
        ("CI", "true", "ci:CI"),
        ("GITHUB_ACTIONS", "true", "ci:GITHUB_ACTIONS"),
        (
            "PYTEST_CURRENT_TEST",
            "tests/unit/test_example.py::test_example",
            "ci:PYTEST_CURRENT_TEST",
        ),
        ("ROAR_JOB_INSTRUMENTED", "1", "roar_job_instrumented"),
        ("RAY_JOB_ID", "job-123", "backend_job_environment:RAY_JOB_ID"),
    ]

    for key, value, reason in cases:
        env = {**base_env, key: value}
        effective = config.resolve_config(environ=env, paths=paths.resolve_paths(env))
        assert not effective.stats_enabled
        assert effective.suppression_reason == reason


def test_suppression_uses_registered_execution_backend_environments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env = _env(tmp_path)

    monkeypatch.setattr(
        config,
        "_execution_backend_job_environment",
        lambda environ: environ.get("CUSTOM_BACKEND_JOB") == "1",
    )

    effective = config.resolve_config(
        environ={**env, "CUSTOM_BACKEND_JOB": "1"},
        paths=paths.resolve_paths(env),
    )

    assert not effective.stats_enabled
    assert effective.suppression_reason == "backend_job_environment:registered"


def test_suppression_when_default_cache_would_dirty_project_tree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    env = {
        "HOME": str(project / "home"),
        "XDG_CONFIG_HOME": str(project / ".xdg"),
    }

    effective = config.resolve_config(
        start_dir=project,
        environ=env,
        paths=paths.resolve_paths(env),
    )

    assert not effective.stats_enabled
    assert effective.suppression_reason == "project_local_cache"


def test_recording_stats_tracks_identity_version_counters_sequence_and_status(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    resolved_paths = paths.resolve_paths(env)

    result = telemetry.record_subcommand("init", environ=env, paths=resolved_paths, version="1.2.3")

    assert result.recorded
    assert result.triggers == ("first_seen_for_install_id", "first_seen_for_version", "init")
    snapshot = stats.read_stats(resolved_paths)
    assert snapshot is not None
    assert snapshot["install_id"]
    assert snapshot["first_seen"]
    assert snapshot["version_milestones"] == {"1.2.3": {"first_seen": snapshot["first_seen"]}}
    assert snapshot["subcommand_counts"]["init"] == 1
    assert snapshot["sequence"] == 0

    run_result = telemetry.record_run_result(
        success=True,
        tracer_backend="ptrace",
        environ=env,
        paths=resolved_paths,
        version="1.2.3",
    )
    assert run_result.triggers == ("first_successful_roar_run",)

    enqueue_result = telemetry.enqueue_trigger(
        "init", environ=env, paths=resolved_paths, version="1.2.3"
    )
    assert enqueue_result.enqueued
    status = telemetry.status_snapshot(environ=env, paths=resolved_paths)
    assert status["enabled"] is True
    assert status["uploads_enabled"] is True
    assert status["queued_event_count"] == 1
    assert status["install_id"] == snapshot["install_id"]


def test_empty_endpoint_preserves_local_stats_but_skips_queueing(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "ROAR_TELEMETRY_ENDPOINT": ""}
    resolved_paths = paths.resolve_paths(env)

    result = telemetry.record_subcommand("init", environ=env, paths=resolved_paths, version="1.2.3")
    assert result.recorded

    enqueue_result = telemetry.enqueue_trigger(
        "init", environ=env, paths=resolved_paths, version="1.2.3"
    )
    assert not enqueue_result.enqueued
    assert enqueue_result.reason == "empty_endpoint"
    assert queue.queued_event_count(resolved_paths) == 0

    snapshot = stats.read_stats(resolved_paths)
    assert snapshot is not None
    assert snapshot["subcommand_counts"]["init"] == 1
    assert snapshot["sequence"] == 0


def test_global_setters_write_resolvable_telemetry_config(tmp_path: Path) -> None:
    env = _env(tmp_path)
    resolved_paths = paths.resolve_paths(env)

    config.set_global_enabled(False, paths=resolved_paths)
    config.set_global_endpoint("", paths=resolved_paths)

    written = resolved_paths.global_config_file.read_text(encoding="utf-8")
    assert "[telemetry]" in written
    effective = config.resolve_config(environ=env, paths=resolved_paths)
    assert not effective.stats_enabled
    assert effective.disabled_reason == "global_disabled"
    assert effective.endpoint == ""


def test_print_payload_returns_inspectable_json_without_mutating_stats(tmp_path: Path) -> None:
    env = _env(tmp_path)
    resolved_paths = paths.resolve_paths(env)

    rendered = telemetry.print_payload(environ=env, paths=resolved_paths, version="1.2.3")

    parsed = json.loads(rendered)
    assert parsed["trigger"] == "preview"
    assert parsed["version"] == "1.2.3"
    assert stats.read_stats(resolved_paths) is None


def test_preview_payload_trigger_is_not_queueable(tmp_path: Path) -> None:
    env = _env(tmp_path)
    resolved_paths = paths.resolve_paths(env)

    rendered = telemetry.print_payload(environ=env, paths=resolved_paths, version="1.2.3")
    assert json.loads(rendered)["trigger"] == "preview"

    try:
        telemetry.enqueue_trigger("preview", environ=env, paths=resolved_paths, version="1.2.3")
    except ValueError as exc:
        assert "unknown telemetry upload trigger" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("preview payloads must remain inspect-only")

    assert queue.queued_event_count(resolved_paths) == 0
    assert stats.read_stats(resolved_paths) is None
