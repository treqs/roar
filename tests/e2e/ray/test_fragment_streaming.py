from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

RAY_DASHBOARD_URL = "http://localhost:8265/api/version"
GLAAS_HEALTH_URL = "http://localhost:3001/api/v1/health"
GLAAS_BASE_URL = "http://localhost:3001"
PLAINTEXT_MARKER = "ROAR_STREAM_PLAINTEXT_MARKER"


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
    (repo_dir / "README.md").write_text("fragment streaming e2e\n", encoding="utf-8")

    _run_checked(["git", "init"], cwd=repo_dir)
    _run_checked(["git", "config", "user.email", "e2e@example.com"], cwd=repo_dir)
    _run_checked(["git", "config", "user.name", "E2E"], cwd=repo_dir)
    _run_checked(["git", "add", "README.md"], cwd=repo_dir)
    _run_checked(["git", "commit", "-m", "init"], cwd=repo_dir)
    _run_checked(
        [sys.executable, "-m", "roar", "init", "--path", str(repo_dir), "-n"], cwd=repo_dir
    )


def _run_file_io_ray_submit(repo_dir: Path) -> dict[str, str]:
    file_io_probe = f"""
import ray

ray.init()

@ray.remote
def io_task():
    marker_path = "/tmp/{PLAINTEXT_MARKER}.txt"
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write("payload")
    with open(marker_path, "r", encoding="utf-8") as handle:
        _ = handle.read()
    return marker_path

print(ray.get(io_task.remote()))
ray.shutdown()
""".strip()

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
            file_io_probe,
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
    return json.loads(key_files[-1].read_text(encoding="utf-8"))


def _fetch_fragments(session_id: str, token: str) -> list[dict[str, object]]:
    token_param = urllib.parse.quote(token, safe="")
    url = f"{GLAAS_BASE_URL}/api/v1/fragments/sessions/{session_id}/fragments?token={token_param}"
    status, body = _http_get(url)
    assert status == 200, f"Expected HTTP 200 from {url}, got {status}. Body: {body}"

    payload = json.loads(body)
    fragments = payload.get("fragments")
    assert isinstance(fragments, list), f"Expected list payload from {url}. Body: {body}"
    return [item for item in fragments if isinstance(item, dict)]


@pytest.mark.e2e
def test_file_io_job_streams_encrypted_fragments_to_glaas(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    key_payload = _run_file_io_ray_submit(repo_dir)
    fragments = _fetch_fragments(key_payload["session_id"], key_payload["token"])

    assert fragments, "Expected at least one streamed fragment batch"
    assert all(isinstance(fragment.get("encrypted_batch"), str) for fragment in fragments)


@pytest.mark.e2e
def test_fragment_list_is_non_empty_for_completed_session(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    key_payload = _run_file_io_ray_submit(repo_dir)
    fragments = _fetch_fragments(key_payload["session_id"], key_payload["token"])

    assert len(fragments) > 0


@pytest.mark.e2e
def test_fragments_are_opaque_ciphertext(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    key_payload = _run_file_io_ray_submit(repo_dir)
    fragments = _fetch_fragments(key_payload["session_id"], key_payload["token"])

    assert fragments, "Expected encrypted fragment batches"
    for fragment in fragments:
        encrypted_batch = str(fragment.get("encrypted_batch") or "")
        assert encrypted_batch
        assert PLAINTEXT_MARKER not in encrypted_batch
        assert f"/tmp/{PLAINTEXT_MARKER}.txt" not in encrypted_batch
