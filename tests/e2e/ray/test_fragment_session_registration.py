from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

RAY_DASHBOARD_URL = "http://localhost:8265/api/version"
GLAAS_HEALTH_URL = "http://localhost:3001/api/v1/health"
GLAAS_BASE_URL = "http://localhost:3001"


def _http_get(url: str, timeout_seconds: int = 5) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        status = int(response.getcode())
        body = response.read().decode("utf-8", errors="replace")
        return status, body


def _skip_if_services_unreachable() -> None:
    checks = (
        ("Ray dashboard", RAY_DASHBOARD_URL),
        ("GLaaS", GLAAS_HEALTH_URL),
    )
    for service_name, url in checks:
        try:
            status, _body = _http_get(url)
        except urllib.error.URLError as exc:
            pytest.skip(f"{service_name} not reachable at {url}: {exc}")
        except (TimeoutError, ConnectionError, OSError) as exc:
            pytest.skip(f"{service_name} not reachable at {url}: {exc}")
        if status != 200:
            pytest.skip(f"{service_name} not healthy at {url}: HTTP {status}")


def _run_checked(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(
            f"Command failed ({' '.join(command)}):\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _init_clean_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("fragment session e2e\n", encoding="utf-8")

    _run_checked(["git", "init"], cwd=repo_dir)
    _run_checked(["git", "config", "user.email", "e2e@example.com"], cwd=repo_dir)
    _run_checked(["git", "config", "user.name", "E2E"], cwd=repo_dir)
    _run_checked(["git", "add", "README.md"], cwd=repo_dir)
    _run_checked(["git", "commit", "-m", "init"], cwd=repo_dir)
    _run_checked(
        [sys.executable, "-m", "roar", "init", "--path", str(repo_dir), "-n"], cwd=repo_dir
    )


def _run_roar_ray_submit(
    repo_dir: Path,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path]:
    probe = (
        "import os; "
        "print('ROAR_SESSION_ID=' + (os.getenv('ROAR_SESSION_ID') or '')); "
        "print('ROAR_FRAGMENT_TOKEN=' + (os.getenv('ROAR_FRAGMENT_TOKEN') or ''))"
    )
    env = dict(os.environ)
    env["GLAAS_URL"] = GLAAS_BASE_URL
    env["GLAAS_API_URL"] = GLAAS_BASE_URL

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            "ray",
            "job",
            "submit",
            "--address",
            "http://localhost:8265",
            "--working-dir",
            ".",
            "--",
            "python3",
            "-c",
            probe,
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
    )

    output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0 and "require the ray[default] installation" in output:
        pytest.skip("Ray job submit requires ray[default] in this environment")
    if result.returncode != 0 and any(
        msg in output
        for msg in (
            "connection refused",
            "failed to connect",
            "unable to connect",
            "cannot connect",
            "timed out",
            "deadline exceeded",
        )
    ):
        pytest.skip("Ray or GLaaS became unreachable during submit")
    if result.returncode != 0:
        pytest.fail(
            f"roar run ray job submit failed.\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )

    fragment_dir = repo_dir / ".roar" / "fragment-sessions"
    key_files = sorted(fragment_dir.glob("*.key"))
    assert key_files, f"Expected at least one key file under {fragment_dir}"
    key_payload = json.loads(key_files[-1].read_text(encoding="utf-8"))
    return result, key_payload, key_files[-1]


@pytest.mark.e2e
def test_roar_ray_submit_creates_fragment_key_file(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    _result, key_payload, key_file = _run_roar_ray_submit(repo_dir)

    assert key_file.exists()
    assert key_file.parent == repo_dir / ".roar" / "fragment-sessions"
    uuid.UUID(str(key_payload["session_id"]))
    assert isinstance(key_payload.get("token"), str) and len(key_payload["token"]) == 64
    assert isinstance(key_payload.get("token_hash"), str) and len(key_payload["token_hash"]) == 64
    assert key_payload.get("created_at")


@pytest.mark.e2e
def test_session_is_preregistered_in_glaas_fragment_store(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    _result, key_payload, _key_file = _run_roar_ray_submit(repo_dir)
    token = urllib.parse.quote(str(key_payload["token"]), safe="")
    session_id = key_payload["session_id"]
    url = f"{GLAAS_BASE_URL}/api/v1/fragments/sessions/{session_id}/fragments?token={token}"
    status, body = _http_get(url)

    assert status == 200, f"Expected HTTP 200 from {url}, got {status}. Body: {body}"
    response_payload = json.loads(body)
    assert "fragments" in response_payload


@pytest.mark.e2e
def test_session_env_vars_visible_inside_ray_job(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    result, key_payload, _key_file = _run_roar_ray_submit(repo_dir)
    output = f"{result.stdout}\n{result.stderr}"

    session_match = re.search(r"ROAR_SESSION_ID=([0-9a-fA-F-]+)", output)
    token_match = re.search(r"ROAR_FRAGMENT_TOKEN=([0-9a-fA-F]+)", output)

    assert session_match is not None, f"ROAR_SESSION_ID not found in output:\n{output}"
    assert token_match is not None, f"ROAR_FRAGMENT_TOKEN not found in output:\n{output}"
    assert session_match.group(1) == key_payload["session_id"]
    assert token_match.group(1) == key_payload["token"]
