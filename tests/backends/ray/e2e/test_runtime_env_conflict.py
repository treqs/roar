from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SUCCESS_MARKER = "RUNTIME_ENV_CONFLICT_OVERRIDE_OK"
_RAY_ADDRESS = "http://localhost:8265"


def _run_checked(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(
            f"Command failed ({' '.join(command)}):\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _init_clean_repo(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "README.md").write_text("runtime env conflict e2e\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")

    _run_checked(["git", "init"], cwd=project_dir)
    _run_checked(["git", "config", "user.email", "e2e@example.com"], cwd=project_dir)
    _run_checked(["git", "config", "user.name", "E2E"], cwd=project_dir)
    _run_checked(["git", "add", "README.md", ".gitignore"], cwd=project_dir)
    _run_checked(["git", "commit", "-m", "init"], cwd=project_dir)
    _run_checked(
        [sys.executable, "-m", "roar", "init", "--path", str(project_dir), "-n"],
        cwd=project_dir,
    )


def _set_project_config(project_dir: Path, *, pip_install: bool) -> None:
    config_path = project_dir / ".roar" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")

    expected_setting = "true" if pip_install else "false"
    if f"pip_install = {expected_setting}" not in config_text:
        replacement = f"pip_install = {expected_setting}"
        if "pip_install = true" in config_text:
            config_text = config_text.replace("pip_install = true", replacement)
        else:
            config_text = config_text.replace("pip_install = false", replacement)
    if 'default = "ptrace"' not in config_text:
        config_text = config_text.replace('default = "auto"', 'default = "ptrace"')
    config_text = config_text.replace('url = "https://api.glaas.ai"', 'url = ""')

    config_path.write_text(config_text, encoding="utf-8")
    assert f"pip_install = {expected_setting}" in config_path.read_text(encoding="utf-8")


def _maybe_skip_for_environment_errors(result: subprocess.CompletedProcess[str]) -> None:
    output = f"{result.stdout}\n{result.stderr}".lower()

    if result.returncode != 0 and "require the ray[default] installation" in output:
        pytest.skip("Ray job submit requires ray[default] in this environment")

    if result.returncode != 0 and any(
        message in output
        for message in (
            "connection refused",
            "failed to connect",
            "unable to connect",
            "cannot connect",
            "timed out",
            "deadline exceeded",
        )
    ):
        pytest.skip("Ray dashboard became unreachable during job submission")


def _run_submit(
    project_dir: Path, *, override_job_runtime_env: bool
) -> subprocess.CompletedProcess[str]:
    candidate = Path(sys.executable).with_name("ray")
    ray_binary = str(candidate) if candidate.exists() else shutil.which("ray")
    if not ray_binary:
        pytest.skip("ray CLI is not available in PATH")

    probe = f"import ray; ray.init(); print('{_SUCCESS_MARKER}'); ray.shutdown()"
    runtime_env: dict[str, object] = {
        "pip": ["pydantic==2.12.5", "pydantic-settings==2.12.0"],
    }

    env = dict(os.environ)
    if override_job_runtime_env:
        env["RAY_OVERRIDE_JOB_RUNTIME_ENV"] = "1"
        runtime_env["env_vars"] = {"RAY_OVERRIDE_JOB_RUNTIME_ENV": "1"}
    else:
        env.pop("RAY_OVERRIDE_JOB_RUNTIME_ENV", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            "--tracer",
            "ptrace",
            ray_binary,
            "job",
            "submit",
            "--address",
            _RAY_ADDRESS,
            "--working-dir",
            ".",
            "--runtime-env-json",
            json.dumps(runtime_env),
            "--",
            "python3",
            "-c",
            probe,
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
    )
    _maybe_skip_for_environment_errors(result)
    return result


@pytest.mark.e2e
def test_runtime_env_with_pip_install_enabled_succeeds_without_override(
    ray_cluster: dict[str, str], tmp_path: Path
) -> None:
    del ray_cluster
    project_dir = tmp_path / "repo"
    _init_clean_repo(project_dir)
    _set_project_config(project_dir, pip_install=True)

    result = _run_submit(project_dir, override_job_runtime_env=False)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        "Expected `roar run ray job submit` to succeed without "
        "RAY_OVERRIDE_JOB_RUNTIME_ENV=1 when ray.pip_install=true.\n"
        f"output:\n{output}"
    )
    assert _SUCCESS_MARKER in output, output


@pytest.mark.e2e
def test_runtime_env_with_pip_install_enabled_also_succeeds_with_override(
    ray_cluster: dict[str, str], tmp_path: Path
) -> None:
    del ray_cluster
    project_dir = tmp_path / "repo"
    _init_clean_repo(project_dir)
    _set_project_config(project_dir, pip_install=True)

    result = _run_submit(project_dir, override_job_runtime_env=True)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        "Expected `roar run ray job submit` to succeed with "
        "RAY_OVERRIDE_JOB_RUNTIME_ENV=1.\n"
        f"output:\n{output}"
    )
    assert _SUCCESS_MARKER in output, output


@pytest.mark.e2e
def test_runtime_env_no_conflict_when_pip_install_disabled(
    ray_cluster: dict[str, str], tmp_path: Path
) -> None:
    del ray_cluster
    project_dir = tmp_path / "repo"
    _init_clean_repo(project_dir)
    _set_project_config(project_dir, pip_install=False)

    result = _run_submit(project_dir, override_job_runtime_env=False)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        "Expected `roar run ray job submit` to succeed without "
        "RAY_OVERRIDE_JOB_RUNTIME_ENV=1 when ray.pip_install=false.\n"
        f"output:\n{output}"
    )
    assert _SUCCESS_MARKER in output, output
