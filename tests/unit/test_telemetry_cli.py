from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from roar.cli import cli
from roar.cli.commands.telemetry import telemetry
from roar.cli.context import RoarContext
from roar.telemetry import paths


def _env(tmp_path: Path) -> dict[str, str]:
    state_root = tmp_path.parent / f"{tmp_path.name}-telemetry-state"
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(state_root / "config"),
        "XDG_CACHE_HOME": str(state_root / "cache"),
        "CI": "",
        "GITHUB_ACTIONS": "",
        "GITLAB_CI": "",
        "BUILDKITE": "",
        "CIRCLECI": "",
        "TRAVIS": "",
        "APPVEYOR": "",
        "TEAMCITY_VERSION": "",
        "JENKINS_URL": "",
        "TF_BUILD": "",
        "BITBUCKET_BUILD_NUMBER": "",
        "CODEBUILD_BUILD_ID": "",
        "DRONE": "",
        "HEROKU_TEST_RUN_ID": "",
        "PYTEST_CURRENT_TEST": "",
    }


def _ctx(tmp_path: Path) -> RoarContext:
    return RoarContext(
        roar_dir=tmp_path / ".roar",
        repo_root=None,
        cwd=tmp_path,
        is_interactive=False,
    )


def test_telemetry_status_does_not_create_install_stats(tmp_path: Path) -> None:
    env = _env(tmp_path)
    runner = CliRunner()

    result = runner.invoke(telemetry, ["--status"], obj=_ctx(tmp_path), env=env)

    assert result.exit_code == 0, result.output
    assert "Telemetry: enabled" in result.output
    assert "Install ID: (not created)" in result.output
    assert not paths.resolve_paths(env).stats_file.exists()


def test_telemetry_print_renders_preview_payload_without_mutating_stats(tmp_path: Path) -> None:
    env = _env(tmp_path)
    runner = CliRunner()

    result = runner.invoke(telemetry, ["--print"], obj=_ctx(tmp_path), env=env)

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["trigger"] == "preview"
    assert parsed["payload_schema_version"] == 1
    assert not paths.resolve_paths(env).stats_file.exists()


def test_telemetry_print_errors_when_env_suppresses_uploads(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "ROAR_NO_TELEMETRY": "1"}
    runner = CliRunner()

    result = runner.invoke(telemetry, ["--print"], obj=_ctx(tmp_path), env=env)

    assert result.exit_code != 0
    assert "No telemetry payload would be uploaded: roar_no_telemetry." in result.output
    assert not paths.resolve_paths(env).stats_file.exists()


def test_telemetry_print_errors_when_endpoint_is_empty(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "ROAR_TELEMETRY_ENDPOINT": ""}
    runner = CliRunner()

    result = runner.invoke(telemetry, ["--print"], obj=_ctx(tmp_path), env=env)

    assert result.exit_code != 0
    assert "No telemetry payload would be uploaded: empty_endpoint." in result.output
    assert not paths.resolve_paths(env).stats_file.exists()


def test_telemetry_disable_enable_and_endpoint_write_global_config(tmp_path: Path) -> None:
    env = _env(tmp_path)
    runner = CliRunner()
    ctx = _ctx(tmp_path)

    disabled = runner.invoke(telemetry, ["--disable"], obj=ctx, env=env)
    assert disabled.exit_code == 0, disabled.output
    config_path = paths.resolve_paths(env).global_config_file
    assert "enabled = false" in config_path.read_text(encoding="utf-8")

    enabled = runner.invoke(telemetry, ["--enable"], obj=ctx, env=env)
    assert enabled.exit_code == 0, enabled.output
    assert "enabled = true" in config_path.read_text(encoding="utf-8")

    endpoint = runner.invoke(
        telemetry,
        ["--endpoint", "https://collector.example/roar"],
        obj=ctx,
        env=env,
    )
    assert endpoint.exit_code == 0, endpoint.output
    assert 'endpoint = "https://collector.example/roar"' in config_path.read_text(
        encoding="utf-8"
    )


def test_init_discloses_telemetry_forbidden_data_categories(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "ROAR_NO_TELEMETRY": "1"}
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["init", "--path", str(project), "--no-gitignore"],
        env=env,
    )

    assert result.exit_code == 0, result.output
    assert "Telemetry payloads do not include" in result.output
    for forbidden in (
        "commands",
        "paths",
        "git remotes",
        "artifact hashes",
        "stdout/stderr",
        "API keys/secrets",
    ):
        assert forbidden in result.output
