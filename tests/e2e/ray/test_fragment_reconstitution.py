from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from roar.ray.fragment_reconstituter import FragmentReconstituter

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
    (repo_dir / "README.md").write_text("fragment reconstitution e2e\n", encoding="utf-8")
    (repo_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")

    _run_checked(["git", "init"], cwd=repo_dir)
    _run_checked(["git", "config", "user.email", "e2e@example.com"], cwd=repo_dir)
    _run_checked(["git", "config", "user.name", "E2E"], cwd=repo_dir)
    _run_checked(["git", "add", "README.md", ".gitignore"], cwd=repo_dir)
    _run_checked(["git", "commit", "-m", "init"], cwd=repo_dir)
    _run_checked(
        [sys.executable, "-m", "roar", "init", "--path", str(repo_dir), "-n"], cwd=repo_dir
    )
    config_path = repo_dir / ".roar" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    config_text = config_text.replace(
        'url = "https://api.glaas.ai"',
        f'url = "{GLAAS_BASE_URL}"',
    )
    config_path.write_text(config_text, encoding="utf-8")


def _run_submit(repo_dir: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path]:
    file_io_probe = """
import os
import ray
from pathlib import Path

os.environ["RAY_OVERRIDE_JOB_RUNTIME_ENV"] = "1"
ray.init()

@ray.remote
def io_task():
    path = "/tmp/roar-fragment-reconstitution-e2e.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("payload")
    with open(path, "r", encoding="utf-8") as handle:
        _ = handle.read()
    return path

print(ray.get(io_task.remote()))
ray.shutdown()
""".strip()

    env = dict(os.environ)
    env["GLAAS_URL"] = GLAAS_BASE_URL
    env["GLAAS_API_URL"] = GLAAS_BASE_URL
    env["RAY_OVERRIDE_JOB_RUNTIME_ENV"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            "--tracer",
            "ptrace",
                "ray",
                "job",
                "submit",
                "--runtime-env-json",
                '{"env_vars":{"AWS_SESSION_TOKEN":"roar-fragment-mode"}}',
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
    key_file = key_files[-1]
    key_payload = json.loads(key_file.read_text(encoding="utf-8"))
    return result, key_payload, key_file


def _fetch_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "jobs": int(
                conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type = 'ray_task'").fetchone()[0]
            ),
            "artifacts": int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]),
            "artifact_hashes": int(
                conn.execute("SELECT COUNT(*) FROM artifact_hashes").fetchone()[0]
            ),
            "job_inputs": int(conn.execute("SELECT COUNT(*) FROM job_inputs").fetchone()[0]),
            "job_outputs": int(conn.execute("SELECT COUNT(*) FROM job_outputs").fetchone()[0]),
        }
    finally:
        conn.close()


@pytest.mark.e2e
def test_auto_reconstitution_populates_local_roar_db(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    result, _key_payload, _key_file = _run_submit(repo_dir)
    db_path = repo_dir / ".roar" / "roar.db"
    counts = _fetch_counts(db_path)

    assert "[roar] lineage reconstituted:" in f"{result.stdout}\n{result.stderr}"
    assert counts["jobs"] > 0
    assert counts["artifacts"] > 0
    assert counts["job_inputs"] + counts["job_outputs"] > 0


@pytest.mark.e2e
def test_reconstituted_artifact_hash_rows_are_present_and_correct(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    _result, _key_payload, _key_file = _run_submit(repo_dir)
    db_path = repo_dir / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ah.algorithm, ah.digest, a.id AS artifact_id
            FROM artifact_hashes ah
            JOIN artifacts a ON a.id = ah.artifact_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows, "Expected artifact_hashes rows to be created during reconstitution"
    for row in rows:
        algorithm = str(row["algorithm"] or "")
        digest = str(row["digest"] or "")
        artifact_id = str(row["artifact_id"] or "")
        assert algorithm
        assert digest
        assert artifact_id
        if algorithm == "blake3":
            assert len(digest) == 64
            int(digest, 16)
        if algorithm == "sha256":
            assert len(digest) == 64
            int(digest, 16)
        if algorithm == "sha512":
            assert len(digest) == 128
            int(digest, 16)
        if algorithm == "md5":
            assert len(digest) == 32
            int(digest, 16)


@pytest.mark.e2e
def test_reconstitution_is_idempotent(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    _result, key_payload, _key_file = _run_submit(repo_dir)
    db_path = repo_dir / ".roar" / "roar.db"
    before = _fetch_counts(db_path)

    second_result = FragmentReconstituter(
        session_id=str(key_payload["session_id"]),
        token=str(key_payload["token"]),
        glaas_url=GLAAS_BASE_URL,
        roar_db_path=db_path,
    ).reconstitute()
    after = _fetch_counts(db_path)

    assert second_result.jobs_merged == 0
    assert second_result.artifacts_merged == 0
    assert before == after


@pytest.mark.e2e
def test_fragment_key_file_is_retained(tmp_path: Path) -> None:
    _skip_if_services_unreachable()
    repo_dir = tmp_path / "repo"
    _init_clean_repo(repo_dir)

    _result, key_payload, key_file = _run_submit(repo_dir)
    db_path = repo_dir / ".roar" / "roar.db"
    assert key_file.exists()

    FragmentReconstituter(
        session_id=str(key_payload["session_id"]),
        token=str(key_payload["token"]),
        glaas_url=GLAAS_BASE_URL,
        roar_db_path=db_path,
    ).reconstitute()

    assert key_file.exists()
