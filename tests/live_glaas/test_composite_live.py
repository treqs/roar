"""
Live GLaaS composite tests for roar put directory registration.

Requires a running glaas-api (default: http://localhost:3001).
Run with:
    ROAR_PUT_SKIP_UPLOAD=1 pytest tests/live_glaas/test_composite_live.py -v -m live_glaas --dist no
"""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from roar.glaas_client import make_auth_header

_DEFAULT_GLAAS_DB_URL = "postgresql://postgres:postgres@localhost:5434/postgres"
_NODE_DB_QUERY_SCRIPT = """
const { Client } = require(process.env.ROAR_GLAAS_PG_MODULE);
const sql = process.env.ROAR_GLAAS_SQL;
const params = JSON.parse(process.env.ROAR_GLAAS_PARAMS || "[]");
const connectionString = process.env.GLAAS_DATABASE_URL || process.env.DATABASE_URL;
if (!connectionString) {
  console.error("Missing GLAAS_DATABASE_URL or DATABASE_URL");
  process.exit(2);
}

const run = async () => {
  const client = new Client({ connectionString });
  try {
    await client.connect();
    const result = await client.query(sql, params);
    process.stdout.write(JSON.stringify(result.rows));
  } catch (error) {
    console.error(error && error.message ? error.message : String(error));
    process.exit(1);
  } finally {
    await client.end().catch(() => {});
  }
};
run();
"""


def _wait_for_health(url: str, timeout_seconds: float = 45.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{url}/api/v1/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for GLaaS health endpoint at {url}: {last_error}")


def _wait_for_health_or_process_exit(
    url: str, process: subprocess.Popen[str], timeout_seconds: float = 45.0
) -> None:
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise RuntimeError(
                f"glaas-api exited before becoming healthy (code={process.returncode}).\n"
                f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
            )
        try:
            req = urllib.request.Request(f"{url}/api/v1/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for GLaaS health endpoint at {url}: {last_error}")


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _with_database(url: str, database_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{database_name}", parts.query, parts.fragment)
    )


@pytest.fixture(scope="session")
def managed_glaas_url() -> str:
    external_url = os.environ.get("GLAAS_URL")
    if external_url:
        _wait_for_health(external_url)
        yield external_url
        return

    glaas_api_dir = _resolve_glaas_api_dir()
    if not glaas_api_dir.exists():
        raise RuntimeError(f"glaas-api directory not found: {glaas_api_dir}")

    base_database_url = os.environ.get("GLAAS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not base_database_url:
        base_database_url = _DEFAULT_GLAAS_DB_URL
    admin_database_url = os.environ.get("GLAAS_ADMIN_DATABASE_URL") or _with_database(
        base_database_url, "postgres"
    )
    db_name = f"glaas_live_{uuid4().hex[:12]}"
    database_url = _with_database(base_database_url, db_name)

    _db_query_rows_on_url(f'CREATE DATABASE "{db_name}"', database_url=admin_database_url)

    port = _reserve_local_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["GLAAS_DATABASE_URL"] = database_url
    env["PORT"] = str(port)
    env.setdefault("NODE_ENV", "development")
    env.setdefault("LOG_LEVEL", "warn")

    subprocess.run(
        ["npx", "prisma", "migrate", "deploy"],
        cwd=glaas_api_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    proc = subprocess.Popen(
        ["npx", "ts-node", "-r", "tsconfig-paths/register", "src/index.ts"],
        cwd=glaas_api_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    previous_db_url = os.environ.get("GLAAS_DATABASE_URL")
    try:
        _wait_for_health_or_process_exit(url, proc)
        os.environ["GLAAS_DATABASE_URL"] = database_url
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if previous_db_url is None:
            os.environ.pop("GLAAS_DATABASE_URL", None)
        else:
            os.environ["GLAAS_DATABASE_URL"] = previous_db_url

        _db_query_rows_on_url(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            params=[db_name],
            database_url=admin_database_url,
        )
        _db_query_rows_on_url(
            f'DROP DATABASE IF EXISTS "{db_name}"', database_url=admin_database_url
        )


@pytest.fixture
def glaas_url(managed_glaas_url: str) -> str:
    return managed_glaas_url


@lru_cache(maxsize=8)
def _composite_endpoints_available(glaas_url: str) -> bool:
    req = urllib.request.Request(
        f"{glaas_url}/api/v1/artifacts/composites",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except HTTPError as exc:
        # Endpoint exists but request is invalid/unauthorized.
        if exc.code in {400, 401}:
            return True
        return exc.code != 404
    except Exception:
        return False


@lru_cache(maxsize=1)
def _resolve_glaas_api_dir() -> Path:
    env_path = os.environ.get("GLAAS_API_DIR")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "glaas-api"


def _db_query_rows(
    sql: str, params: list[Any] | None = None, database_url: str | None = None
) -> list[dict[str, Any]]:
    return _db_query_rows_on_url(sql, params=params, database_url=database_url)


def _db_query_rows_on_url(
    sql: str, params: list[Any] | None = None, database_url: str | None = None
) -> list[dict[str, Any]]:
    glaas_api_dir = _resolve_glaas_api_dir()
    pg_module_path = glaas_api_dir / "node_modules" / "pg"
    if not pg_module_path.exists():
        raise RuntimeError(f"pg module not found at {pg_module_path}")

    env = os.environ.copy()
    env["ROAR_GLAAS_SQL"] = sql
    env["ROAR_GLAAS_PARAMS"] = json.dumps(params or [])
    env["ROAR_GLAAS_PG_MODULE"] = str(pg_module_path)
    env["GLAAS_DATABASE_URL"] = (
        database_url
        or os.environ.get("GLAAS_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_GLAAS_DB_URL
    )

    result = subprocess.run(
        ["node", "-e", _NODE_DB_QUERY_SCRIPT],
        cwd=glaas_api_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Node DB query failed")

    payload = result.stdout.strip()
    return json.loads(payload) if payload else []


@pytest.fixture
def glaas_db_queryable(glaas_url: str):
    try:
        rows = _db_query_rows("SELECT 1 AS ok")
    except Exception as exc:
        raise AssertionError(f"GLaaS database query unavailable for {glaas_url}: {exc}") from exc

    assert rows and str(rows[0].get("ok")) == "1", f"Unexpected GLaaS database probe result: {rows}"


@pytest.fixture
def composite_api_available(glaas_url: str):
    assert _composite_endpoints_available(glaas_url), (
        f"Composite endpoints are not available on {glaas_url}. "
        "This indicates a functional gap in the running glaas-api."
    )


@pytest.fixture
def skip_upload_env():
    """Skip actual cloud uploads during put while keeping registration paths."""
    previous = os.environ.get("ROAR_PUT_SKIP_UPLOAD")
    os.environ["ROAR_PUT_SKIP_UPLOAD"] = "1"
    yield
    if previous is None:
        del os.environ["ROAR_PUT_SKIP_UPLOAD"]
    else:
        os.environ["ROAR_PUT_SKIP_UPLOAD"] = previous


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create temporary git repo with roar initialized and tmp filtering disabled."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    (tmp_path / ".gitignore").write_text("")
    (tmp_path / "README.md").write_text("# Composite Live Test\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [sys.executable, "-m", "roar", "init", "-y"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initialize roar"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "filters.ignore_tmp_files", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def glaas_configured(temp_git_repo: Path, glaas_url: str) -> Path:
    """Set GLAAS URL in roar config for this temp repo."""
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "glaas.url", glaas_url],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    # Ensure there is an active session before roar put.
    subprocess.run(
        [sys.executable, "-m", "roar", "run", sys.executable, "-c", "print('session bootstrap')"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


def _api_get(glaas_url: str, api_path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"{glaas_url}{api_path}")
    auth_header = make_auth_header("GET", api_path)
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"GET {api_path} failed with HTTP {exc.code}: {body}") from exc


def _api_post(glaas_url: str, api_path: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{glaas_url}{api_path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    auth_header = make_auth_header("POST", api_path, body=data)
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"POST {api_path} failed with HTTP {exc.code}: {body_text}") from exc


def _get_active_session_row(repo: Path) -> tuple[int, str]:
    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, hash FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row or not row[0] or not row[1]:
        raise RuntimeError("No active session found in roar.db")
    return int(row[0]), str(row[1])


def _get_active_session_hash(repo: Path) -> str:
    _session_id, session_hash = _get_active_session_row(repo)
    return session_hash


def _compute_put_session_hash(repo: Path) -> str:
    session_id, _session_hash = _get_active_session_row(repo)
    roar_dir = (repo / ".roar").resolve()
    return hashlib.sha256(f"{roar_dir}:{session_id}".encode()).hexdigest()


def _register_server_session(glaas_url: str, repo: Path, session_hash: str) -> None:
    git_repo = (
        subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "https://github.com/test/repo.git"
    )
    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    git_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    response = _api_post(
        glaas_url,
        "/api/v1/sessions",
        {
            "hash": session_hash,
            "git_repo": git_repo,
            "git_commit": git_commit,
            "git_branch": git_branch,
        },
    )
    assert response.get("success") is True, f"Session registration failed: {response}"


def _session_composite_rows(session_hash: str) -> list[dict[str, Any]]:
    return _db_query_rows(
        """
        SELECT
          cm.artifact_hash,
          cm.component_count_total,
          cm.component_count_stored,
          a.registered_at
        FROM composite_metadata cm
        JOIN artifacts a ON a.hash = cm.artifact_hash
        WHERE a.original_session_hash = $1
        ORDER BY a.registered_at DESC
        """,
        [session_hash],
    )


def _latest_step_number(repo: Path) -> int:
    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT MAX(step_number) FROM jobs").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("No jobs found in roar.db")
    return int(row[0])


def _local_composite_hashes_for_paths(repo: Path, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}

    placeholders = ", ".join("?" for _ in paths)
    query = f"""
        SELECT a.first_seen_path, ah.digest
        FROM artifacts a
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE a.kind = 'composite'
          AND ah.algorithm = 'composite-blake3'
          AND a.first_seen_path IN ({placeholders})
    """

    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query, paths).fetchall()

    return {str(path): str(digest) for path, digest in rows if path and digest}


def _assert_put_completed_or_registration_error(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    stderr = result.stderr or ""
    assert "Registration completed with errors" in stderr, (
        f"Put failed unexpectedly with return code {result.returncode}: {stderr}"
    )


@pytest.mark.live_glaas
def test_put_small_directory_registers_composite_with_metadata(
    glaas_configured: Path,
    skip_upload_env,
    composite_api_available,
    glaas_db_queryable,
    glaas_url: str,
):
    """roar put on small dir should register composite, populating both tables."""
    repo = glaas_configured
    session_hash = _compute_put_session_hash(repo)
    _register_server_session(glaas_url, repo, session_hash)
    before_rows = _session_composite_rows(session_hash)
    before_hashes = {row["artifact_hash"] for row in before_rows}
    dataset_dir = repo / "small_dataset"
    dataset_dir.mkdir()
    run_nonce = uuid4().hex

    for index in range(3):
        file_path = dataset_dir / f"data_{index}.txt"
        file_path.write_text(f'{{"id": {index}, "run": "{run_nonce}"}}\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add small dataset for composite test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "put",
            "small_dataset",
            "s3://test-bucket/small-dataset",
            "-m",
            "publish small dataset",
            "--no-tag",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    _assert_put_completed_or_registration_error(result)
    after_rows = _session_composite_rows(session_hash)
    new_rows = [row for row in after_rows if row["artifact_hash"] not in before_hashes]
    assert new_rows, f"No composite rows created by put. stderr={result.stderr}"
    composite_row = next((row for row in new_rows if int(row["component_count_total"]) == 3), None)
    assert composite_row is not None, (
        f"No 3-component composite row found after put. new_rows={new_rows}, stderr={result.stderr}"
    )
    composite_hash = composite_row["artifact_hash"]

    # Verify isComposite=true from public endpoint (proves composite_metadata row exists)
    public_data = _api_get(glaas_url, f"/api/v1/public/artifacts/{composite_hash}")
    assert public_data.get("success") is True, f"Public API failed: {public_data}"
    assert public_data["data"]["isComposite"] is True, (
        f"isComposite should be true: {public_data['data']}"
    )

    # Verify components (proves composite_component rows exist)
    components_data = _api_get(glaas_url, f"/api/v1/artifacts/{composite_hash[:16]}/components")
    assert components_data.get("success") is True, f"Components API failed: {components_data}"
    components = components_data["data"]["components"]
    assert len(components) == 3, f"Expected 3 components, got {len(components)}: {components}"
    for comp in components:
        assert comp["componentAlgorithm"] == "blake3"
        assert comp["leafKind"] == "file"
        assert comp["relativePath"]  # non-empty

    artifact_rows = _db_query_rows(
        """
        SELECT hash, source_type, original_session_hash, size::text AS size
        FROM artifacts
        WHERE hash = $1
        """,
        [composite_hash],
    )
    assert len(artifact_rows) == 1, f"Missing artifact row for composite hash {composite_hash}"
    artifact_row = artifact_rows[0]
    assert artifact_row["hash"] == composite_hash
    assert artifact_row["original_session_hash"] == session_hash

    metadata_rows = _db_query_rows(
        """
        SELECT
          component_count_total,
          component_count_stored,
          membership_index IS NOT NULL AS has_membership,
          membership_index ->> 'totalComponents' AS membership_total,
          membership_index ->> 'storedComponents' AS membership_stored,
          membership_index ->> 'bloomFilterBase64' AS bloom_filter,
          membership_index ->> 'bloomBits' AS bloom_bits,
          membership_index ->> 'bloomHashes' AS bloom_hashes,
          membership_index ->> 'bloomVersion' AS bloom_version
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [composite_hash],
    )
    assert len(metadata_rows) == 1, f"Missing composite_metadata row for {composite_hash}"
    metadata_row = metadata_rows[0]
    assert int(metadata_row["component_count_total"]) == 3
    assert int(metadata_row["component_count_stored"]) == 3
    assert metadata_row["has_membership"] is True
    assert int(metadata_row["membership_total"]) == 3
    assert int(metadata_row["membership_stored"]) == 3
    assert metadata_row["bloom_filter"] is not None
    assert int(metadata_row["bloom_bits"]) > 0
    assert int(metadata_row["bloom_hashes"]) > 0
    assert int(metadata_row["bloom_version"]) == 1

    component_rows = _db_query_rows(
        """
        SELECT relative_path
        FROM composite_components
        WHERE composite_hash = $1
        ORDER BY relative_path ASC
        """,
        [composite_hash],
    )
    assert [row["relative_path"] for row in component_rows] == [
        "data_0.txt",
        "data_1.txt",
        "data_2.txt",
    ]

    job_output_rows = _db_query_rows(
        """
        SELECT COUNT(*)::int AS output_count
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE jo.artifact_hash = $1
          AND j.session_hash = $2
        """,
        [composite_hash, session_hash],
    )
    assert int(job_output_rows[0]["output_count"]) >= 1


@pytest.mark.live_glaas
def test_direct_api_composite_registration_populates_metadata(
    glaas_configured: Path,
    composite_api_available,
    glaas_db_queryable,
    glaas_url: str,
):
    """Direct API POST creates composite rows; proves API works independent of roar."""
    repo = glaas_configured
    # Ensure the currently active local session is also present server-side.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            sys.executable,
            "-c",
            "print('composite api bootstrap')",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    session_hash = _get_active_session_hash(repo)
    _register_server_session(glaas_url, repo, session_hash)

    # Generate unique hashes for this test run
    nonce = uuid4().hex
    composite_hash = hashlib.blake2b(nonce.encode(), digest_size=32).hexdigest()

    payload = {
        "hash": composite_hash,
        "size": 3072,
        "source_type": None,
        "session_hash": session_hash,
        "component_count_total": 2,
        "components": [
            {
                "relative_path": "alpha.txt",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": hashlib.blake2b(
                    b"alpha" + nonce.encode(), digest_size=32
                ).hexdigest(),
                "component_size": 1024,
            },
            {
                "relative_path": "beta.txt",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": hashlib.blake2b(
                    b"beta" + nonce.encode(), digest_size=32
                ).hexdigest(),
                "component_size": 2048,
            },
        ],
        "membership_index": {
            "total_components": 2,
            "stored_components": 2,
            "bloom_filter_base64": "AQIDBA==",
            "bloom_bits": 2048,
            "bloom_hashes": 12,
            "bloom_version": 1,
        },
    }

    # POST composite directly to API (bypasses roar CLI)
    post_data = _api_post(glaas_url, "/api/v1/artifacts/composites", payload)
    assert post_data.get("success") is True, f"POST failed: {post_data}"
    assert post_data["data"]["created"] is True, f"Expected created=true: {post_data}"

    # Verify isComposite=true from public endpoint (proves composite_metadata row exists)
    public_data = _api_get(glaas_url, f"/api/v1/public/artifacts/{composite_hash}")
    assert public_data.get("success") is True, f"Public API failed: {public_data}"
    assert public_data["data"]["isComposite"] is True, (
        f"isComposite should be true: {public_data['data']}"
    )

    # Verify components (proves composite_component rows exist)
    components_data = _api_get(glaas_url, f"/api/v1/artifacts/{composite_hash[:16]}/components")
    assert components_data.get("success") is True, f"Components API failed: {components_data}"
    components = components_data["data"]["components"]
    assert len(components) == 2, f"Expected 2 components, got {len(components)}"
    paths = {c["relativePath"] for c in components}
    assert paths == {"alpha.txt", "beta.txt"}, f"Unexpected paths: {paths}"
    for comp in components:
        assert comp["componentAlgorithm"] == "blake3"
        assert comp["leafKind"] == "file"

    artifact_rows = _db_query_rows(
        """
        SELECT hash, source_type, original_session_hash, size::text AS size
        FROM artifacts
        WHERE hash = $1
        """,
        [composite_hash],
    )
    assert len(artifact_rows) == 1, f"Missing artifact row for {composite_hash}"
    assert artifact_rows[0]["original_session_hash"] == session_hash
    assert int(artifact_rows[0]["size"]) == 3072
    assert artifact_rows[0]["source_type"] is None

    metadata_rows = _db_query_rows(
        """
        SELECT
          component_count_total,
          component_count_stored,
          membership_index IS NOT NULL AS has_membership,
          membership_index ->> 'totalComponents' AS membership_total,
          membership_index ->> 'storedComponents' AS membership_stored,
          membership_index ->> 'bloomFilterBase64' AS bloom_filter,
          membership_index ->> 'bloomBits' AS bloom_bits,
          membership_index ->> 'bloomHashes' AS bloom_hashes,
          membership_index ->> 'bloomVersion' AS bloom_version
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [composite_hash],
    )
    assert len(metadata_rows) == 1
    assert int(metadata_rows[0]["component_count_total"]) == 2
    assert int(metadata_rows[0]["component_count_stored"]) == 2
    assert metadata_rows[0]["has_membership"] is True
    assert int(metadata_rows[0]["membership_total"]) == 2
    assert int(metadata_rows[0]["membership_stored"]) == 2
    assert metadata_rows[0]["bloom_filter"] is not None
    assert int(metadata_rows[0]["bloom_bits"]) > 0
    assert int(metadata_rows[0]["bloom_hashes"]) > 0
    assert int(metadata_rows[0]["bloom_version"]) == 1

    component_rows = _db_query_rows(
        """
        SELECT relative_path, component_algorithm, leaf_kind, component_size::text AS component_size
        FROM composite_components
        WHERE composite_hash = $1
        ORDER BY relative_path ASC
        """,
        [composite_hash],
    )
    assert [row["relative_path"] for row in component_rows] == ["alpha.txt", "beta.txt"]
    for row in component_rows:
        assert row["component_algorithm"] == "blake3"
        assert row["leaf_kind"] == "file"


@pytest.mark.live_glaas
def test_put_directory_registers_large_composite_with_bloom_membership(
    glaas_configured: Path,
    skip_upload_env,
    composite_api_available,
    glaas_db_queryable,
    glaas_url: str,
):
    """roar put on large dir should register composite with bloom membership index."""
    repo = glaas_configured
    session_hash = _compute_put_session_hash(repo)
    _register_server_session(glaas_url, repo, session_hash)
    before_rows = _session_composite_rows(session_hash)
    before_hashes = {row["artifact_hash"] for row in before_rows}
    dataset_dir = repo / "large_dataset"
    dataset_dir.mkdir()
    run_nonce = uuid4().hex

    # >1000 leaves to trigger membership index + bloom payload
    for index in range(1105):
        file_path = dataset_dir / f"sample_{index:04d}.jsonl"
        file_path.write_text(f'{{"id": {index}, "value": {index * 2}, "run": "{run_nonce}"}}\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add large dataset for composite test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "put",
            "large_dataset",
            "s3://test-bucket/large-dataset",
            "-m",
            "publish large dataset",
            "--no-tag",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    _assert_put_completed_or_registration_error(result)
    after_rows = _session_composite_rows(session_hash)
    new_rows = [row for row in after_rows if row["artifact_hash"] not in before_hashes]
    assert new_rows, f"No composite rows created by put. stderr={result.stderr}"
    composite_row = next(
        (row for row in new_rows if int(row["component_count_total"]) > 1000), None
    )
    assert composite_row is not None, (
        f"No large composite row found after put. new_rows={new_rows}, stderr={result.stderr}"
    )
    composite_hash = composite_row["artifact_hash"]
    component_count_total = int(composite_row["component_count_total"])
    component_count_stored = int(composite_row["component_count_stored"])
    assert component_count_stored <= 1000

    api_data = _api_get(glaas_url, f"/api/v1/artifacts/{composite_hash[:16]}/components")
    assert api_data.get("success") is True, f"Unexpected API response: {api_data}"

    response = api_data["data"]
    membership = response.get("membership")
    assert membership is not None, f"Membership missing from response: {response}"
    assert int(membership["totalComponents"]) == component_count_total
    assert int(membership["storedComponents"]) == component_count_stored
    assert membership["storedComponents"] <= 1000
    assert membership["bloomBits"] is not None
    assert membership["bloomHashes"] is not None
    assert membership["bloomVersion"] == 1

    metadata_rows = _db_query_rows(
        """
        SELECT
          component_count_total,
          component_count_stored,
          membership_index ->> 'totalComponents' AS membership_total,
          membership_index ->> 'storedComponents' AS membership_stored,
          membership_index ->> 'bloomFilterBase64' AS bloom_filter,
          membership_index ->> 'bloomBits' AS bloom_bits,
          membership_index ->> 'bloomHashes' AS bloom_hashes,
          membership_index ->> 'bloomVersion' AS bloom_version
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [composite_hash],
    )
    assert len(metadata_rows) == 1, f"Missing composite_metadata row for {composite_hash}"
    db_membership = metadata_rows[0]
    assert int(db_membership["component_count_total"]) == component_count_total
    assert int(db_membership["component_count_stored"]) == component_count_stored
    assert int(db_membership["membership_total"]) == component_count_total
    assert int(db_membership["membership_stored"]) == component_count_stored
    assert db_membership["bloom_filter"] is not None
    assert int(db_membership["bloom_bits"]) > 0
    assert int(db_membership["bloom_hashes"]) > 0
    assert int(db_membership["bloom_version"]) == 1

    component_rows = _db_query_rows(
        """
        SELECT COUNT(*)::int AS component_count
        FROM composite_components
        WHERE composite_hash = $1
        """,
        [composite_hash],
    )
    assert int(component_rows[0]["component_count"]) == component_count_stored


@pytest.mark.live_glaas
def test_put_job_reference_registers_upstream_lineage_composites(
    glaas_configured: Path,
    skip_upload_env,
    composite_api_available,
    glaas_db_queryable,
    glaas_url: str,
):
    """
    put @N should pre-register lineage composites so link phases do not 404.

    This reproduces the branch mission directly: upstream composite artifacts
    must be registered and available to the API/web read paths.
    """
    repo = glaas_configured
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    raw_script = scripts_dir / "build_raw.py"
    extract_script = scripts_dir / "build_extracted.py"

    raw_script.write_text(
        """
from pathlib import Path
import json

raw_dir = Path("data/raw")
raw_dir.mkdir(parents=True, exist_ok=True)
for idx in range(8):
    (raw_dir / f"raw_{idx}.json").write_text(json.dumps({"id": idx, "stage": "raw"}) + "\\n")
print("raw done")
""".strip()
        + "\n"
    )
    extract_script.write_text(
        """
from pathlib import Path
import json

raw_dir = Path("data/raw")
extract_dir = Path("data/extracted_lance")
extract_dir.mkdir(parents=True, exist_ok=True)

rows = []
for item in sorted(raw_dir.glob("*.json")):
    payload = json.loads(item.read_text())
    rows.append(payload)
    (extract_dir / f"{item.stem}_proc.json").write_text(
        json.dumps({"id": payload["id"], "stage": "extracted"}) + "\\n"
    )

manifest_path = Path("data/training/ray_manifest.json")
manifest_path.parent.mkdir(parents=True, exist_ok=True)
manifest_path.write_text(json.dumps({"count": len(rows), "source": "data/raw"}) + "\\n")
print("extract done")
""".strip()
        + "\n"
    )

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add lineage e2e scripts"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            sys.executable,
            "scripts/build_raw.py",
            "--output-dir",
            "data/raw",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Record raw outputs"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "run",
            sys.executable,
            "scripts/build_extracted.py",
            "--input-dir",
            "data/raw",
            "--output-dir",
            "data/extracted_lance",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Record extracted outputs"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    raw_path = str((repo / "data" / "raw").resolve())
    extracted_path = str((repo / "data" / "extracted_lance").resolve())
    local_hashes_by_path = _local_composite_hashes_for_paths(repo, [raw_path, extracted_path])
    assert raw_path in local_hashes_by_path, (
        f"Expected local composite hash for data/raw before put. hashes={local_hashes_by_path}"
    )
    assert extracted_path in local_hashes_by_path, (
        "Expected local composite hash for data/extracted_lance before put. "
        f"hashes={local_hashes_by_path}"
    )

    step_number = _latest_step_number(repo)
    session_hash = _compute_put_session_hash(repo)
    _register_server_session(glaas_url, repo, session_hash)

    put_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "roar",
            "put",
            f"@{step_number}",
            "s3://test-bucket/lineage-reference",
            "-m",
            "publish lineage reference",
            "--no-tag",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert put_result.returncode == 0, (
        "put @N failed; lineage composite registration should have prevented unresolved "
        f"artifact warnings/errors.\nstdout:\n{put_result.stdout}\n\nstderr:\n{put_result.stderr}"
    )

    for composite_path, composite_hash in local_hashes_by_path.items():
        public_data = _api_get(glaas_url, f"/api/v1/public/artifacts/{composite_hash}")
        assert public_data.get("success") is True, f"Public artifact lookup failed: {public_data}"
        assert public_data["data"]["isComposite"] is True, (
            f"Expected isComposite=true for {composite_hash} ({composite_path})"
        )

        components_data = _api_get(glaas_url, f"/api/v1/artifacts/{composite_hash[:16]}/components")
        assert components_data.get("success") is True, (
            f"Composite components lookup failed for {composite_hash}: {components_data}"
        )
        assert len(components_data["data"]["components"]) >= 1

        metadata_rows = _db_query_rows(
            """
            SELECT component_count_total, component_count_stored
            FROM composite_metadata
            WHERE artifact_hash = $1
            """,
            [composite_hash],
        )
        assert metadata_rows, f"Missing composite_metadata for {composite_hash} ({composite_path})"
        assert int(metadata_rows[0]["component_count_total"]) >= 1

        put_input_rows = _db_query_rows(
            """
            SELECT COUNT(*)::int AS input_count
            FROM job_inputs ji
            JOIN jobs j ON j.id = ji.job_id
            WHERE j.session_hash = $1
              AND j.job_type = 'put'
              AND ji.artifact_hash = $2
            """,
            [session_hash, composite_hash],
        )
        assert int(put_input_rows[0]["input_count"]) >= 1, (
            f"Expected put job input link for composite {composite_hash} ({composite_path})"
        )
