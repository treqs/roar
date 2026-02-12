"""
Integration tests for the S3 proxy feature.

Verifies the full end-to-end flow: proxy starts during ``roar run``,
intercepts S3 traffic via urllib.request, forwards to a FakeS3Server,
and records artifacts in the database.
"""

import hashlib
import platform
import sqlite3
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="ptrace tracer is Linux-only"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def query_db(repo_path, sql, params=()):
    """Open .roar/roar.db, execute *sql*, return list of sqlite3.Row."""
    db_path = repo_path / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def latest_job_id(repo_path):
    """Return the most recent job id from the database."""
    rows = query_db(repo_path, "SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
    assert rows, "No job found in the database"
    return rows[0]["id"]


def expected_etag(content: bytes) -> str:
    """Compute the unquoted md5 hex digest that roar stores for an etag."""
    return hashlib.md5(content).hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProxyCapturesS3:
    """Tests that verify proxy records S3 operations in the database."""

    def test_proxy_captures_s3_put_as_job_output(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """PUT to S3 is recorded as a job output with correct etag."""
        script = proxy_repo / "s3_put.py"
        script.write_text(
            "import os, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            "req = urllib.request.Request(\n"
            '    f"{endpoint}/test-bucket/output.csv",\n'
            '    data=b"result_data",\n'
            '    method="PUT",\n'
            ")\n"
            "urllib.request.urlopen(req)\n"
        )
        git_commit("add s3 put script")

        result = roar_cli("run", python_exe, "s3_put.py", check=False)
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        job_id = latest_job_id(proxy_repo)

        # Check job_outputs for the S3 path
        rows = query_db(
            proxy_repo,
            """
            SELECT jo.path, a.source_type
            FROM job_outputs jo
            JOIN artifacts a ON jo.artifact_id = a.id
            WHERE jo.job_id = ? AND a.source_type = 's3'
            """,
            (job_id,),
        )
        paths = [r["path"] for r in rows]
        assert "s3://test-bucket/output.csv" in paths

        # Check etag
        rows = query_db(
            proxy_repo,
            """
            SELECT ah.algorithm, ah.digest
            FROM job_outputs jo
            JOIN artifact_hashes ah ON jo.artifact_id = ah.artifact_id
            WHERE jo.job_id = ? AND ah.algorithm = 'etag'
              AND jo.path = 's3://test-bucket/output.csv'
            """,
            (job_id,),
        )
        assert len(rows) >= 1
        assert rows[0]["digest"] == expected_etag(b"result_data")

    def test_proxy_captures_s3_get_as_job_input(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """GET from S3 is recorded as a job input with correct etag."""
        fake_s3.seed("test-bucket", "input.csv", b"training_data")

        script = proxy_repo / "s3_get.py"
        script.write_text(
            "import os, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            'urllib.request.urlopen(f"{endpoint}/test-bucket/input.csv")\n'
        )
        git_commit("add s3 get script")

        result = roar_cli("run", python_exe, "s3_get.py", check=False)
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        job_id = latest_job_id(proxy_repo)

        rows = query_db(
            proxy_repo,
            """
            SELECT ji.path, a.source_type
            FROM job_inputs ji
            JOIN artifacts a ON ji.artifact_id = a.id
            WHERE ji.job_id = ? AND a.source_type = 's3'
            """,
            (job_id,),
        )
        paths = [r["path"] for r in rows]
        assert "s3://test-bucket/input.csv" in paths

        # Check etag
        rows = query_db(
            proxy_repo,
            """
            SELECT ah.digest
            FROM job_inputs ji
            JOIN artifact_hashes ah ON ji.artifact_id = ah.artifact_id
            WHERE ji.job_id = ? AND ah.algorithm = 'etag'
              AND ji.path = 's3://test-bucket/input.csv'
            """,
            (job_id,),
        )
        assert len(rows) >= 1
        assert rows[0]["digest"] == expected_etag(b"training_data")

    def test_proxy_captures_mixed_get_and_put(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """Both GET (input) and PUT (output) in a single run are recorded."""
        fake_s3.seed("mix-bucket", "src.csv", b"source_data")

        script = proxy_repo / "s3_mix.py"
        script.write_text(
            "import os, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            "# Read input\n"
            'urllib.request.urlopen(f"{endpoint}/mix-bucket/src.csv")\n'
            "# Write output\n"
            "req = urllib.request.Request(\n"
            '    f"{endpoint}/mix-bucket/dst.csv",\n'
            '    data=b"derived_data",\n'
            '    method="PUT",\n'
            ")\n"
            "urllib.request.urlopen(req)\n"
        )
        git_commit("add s3 mix script")

        result = roar_cli("run", python_exe, "s3_mix.py", check=False)
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        job_id = latest_job_id(proxy_repo)

        # Input
        inputs = query_db(
            proxy_repo,
            "SELECT path FROM job_inputs WHERE job_id = ?",
            (job_id,),
        )
        input_paths = [r["path"] for r in inputs]
        assert "s3://mix-bucket/src.csv" in input_paths

        # Output
        outputs = query_db(
            proxy_repo,
            "SELECT path FROM job_outputs WHERE job_id = ?",
            (job_id,),
        )
        output_paths = [r["path"] for r in outputs]
        assert "s3://mix-bucket/dst.csv" in output_paths

        # Distinct artifact IDs
        all_artifacts = query_db(
            proxy_repo,
            """
            SELECT artifact_id FROM job_inputs WHERE job_id = ? AND path LIKE 's3://%'
            UNION ALL
            SELECT artifact_id FROM job_outputs WHERE job_id = ? AND path LIKE 's3://%'
            """,
            (job_id, job_id),
        )
        artifact_ids = [r["artifact_id"] for r in all_artifacts]
        assert len(set(artifact_ids)) >= 2, (
            f"Expected distinct artifact IDs for input vs output, got {artifact_ids}"
        )


class TestProxyChaining:
    """Tests that verify proxy chains requests to the upstream (fake S3)."""

    def test_proxy_chains_existing_aws_endpoint_url(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """Verify the fake S3 actually received the request through the proxy chain."""
        initial_count = len(fake_s3.requests)

        script = proxy_repo / "s3_chain.py"
        script.write_text(
            "import os, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            "req = urllib.request.Request(\n"
            '    f"{endpoint}/chain-test/data.csv",\n'
            '    data=b"chain_payload",\n'
            '    method="PUT",\n'
            ")\n"
            "urllib.request.urlopen(req)\n"
        )
        git_commit("add chain test script")

        result = roar_cli("run", python_exe, "s3_chain.py", check=False)
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        # Check that the fake S3 received the PUT (proving: child → proxy → fake S3)
        new_requests = fake_s3.requests[initial_count:]
        put_paths = [path for method, path, _ in new_requests if method == "PUT"]
        assert any("/chain-test/data.csv" in p for p in put_paths), (
            f"Expected PUT to /chain-test/data.csv in fake S3 requests, got: {new_requests}"
        )


class TestProxyEtags:
    """Tests that verify etag faithfulness."""

    def test_proxy_etags_match_server_response(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """Etag stored in artifact_hashes matches the md5 from fake S3."""
        content = b"etag_verification_content"
        fake_s3.seed("etag-bucket", "verify.dat", content)

        script = proxy_repo / "s3_etag.py"
        script.write_text(
            "import os, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            'urllib.request.urlopen(f"{endpoint}/etag-bucket/verify.dat")\n'
        )
        git_commit("add etag verify script")

        result = roar_cli("run", python_exe, "s3_etag.py", check=False)
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        job_id = latest_job_id(proxy_repo)
        rows = query_db(
            proxy_repo,
            """
            SELECT ah.digest
            FROM job_inputs ji
            JOIN artifact_hashes ah ON ji.artifact_id = ah.artifact_id
            WHERE ji.job_id = ? AND ah.algorithm = 'etag'
              AND ji.path = 's3://etag-bucket/verify.dat'
            """,
            (job_id,),
        )
        assert len(rows) >= 1
        assert rows[0]["digest"] == expected_etag(content)


class TestProxyGracefulDegradation:
    """Tests that verify proxy failure modes."""

    def test_run_fails_when_proxy_enabled_but_binary_missing(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
    ):
        """roar run exits with error when proxy enabled but binary not found.

        We simulate a missing binary by pointing ProxyService's package_path
        to a temporary location where no proxy binary exists, while keeping
        PATH set to only the directories needed for git (so roar's git
        validation still works).
        """
        import os
        import shutil
        import subprocess

        # Enable proxy in config
        config_path = temp_git_repo / ".roar" / "config.toml"
        config_text = config_path.read_text()
        config_text += "\n[proxy]\nenabled = true\n"
        config_path.write_text(config_text)

        script = temp_git_repo / "hello.py"
        script.write_text('print("hello")\n')
        git_commit("enable proxy and add script")

        # We inject a sitecustomize that monkeypatches find_proxy → None
        # so the roar subprocess thinks the binary is missing.
        # Place it *outside* the git repo so __pycache__ doesn't dirty it.
        customise_dir = temp_git_repo.parent / "_sitecustomize"
        customise_dir.mkdir(exist_ok=True)
        (customise_dir / "sitecustomize.py").write_text(
            "from roar.services.execution.proxy import ProxyService\n"
            "ProxyService.find_proxy = lambda self: None\n"
        )

        # Build a minimal PATH: keep only directories that contain 'git'
        git_path = shutil.which("git")
        git_dir = os.path.dirname(git_path) if git_path else "/usr/bin"

        env = dict(os.environ)
        env["PATH"] = git_dir
        # Prepend our sitecustomize directory so it runs on import
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{customise_dir}:{existing}" if existing else str(customise_dir)

        result = subprocess.run(
            [sys.executable, "-m", "roar", "run", sys.executable, "hello.py"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            env=env,
        )
        # The CLI exits 1 when proxy is enabled but binary not found
        assert result.returncode == 1
        combined = (result.stderr + result.stdout).lower()
        assert "proxy" in combined, (
            f"Expected 'proxy' in output:\nstdout={result.stdout}\nstderr={result.stderr}"
        )


class TestProxyOnScriptFailure:
    """Tests that verify proxy captures artifacts even when the child fails."""

    def test_proxy_captures_operations_on_script_failure(
        self,
        proxy_repo,
        roar_cli,
        git_commit,
        fake_s3,
        python_exe,
    ):
        """S3 artifacts are still recorded when the child script exits non-zero."""
        script = proxy_repo / "s3_fail.py"
        script.write_text(
            "import os, sys, urllib.request\n"
            'endpoint = os.environ["AWS_ENDPOINT_URL"]\n'
            "req = urllib.request.Request(\n"
            '    f"{endpoint}/fail-bucket/before_crash.csv",\n'
            '    data=b"partial_output",\n'
            '    method="PUT",\n'
            ")\n"
            "urllib.request.urlopen(req)\n"
            "sys.exit(1)\n"
        )
        git_commit("add failing s3 script")

        result = roar_cli("run", python_exe, "s3_fail.py", check=False)
        assert result.returncode != 0

        job_id = latest_job_id(proxy_repo)
        rows = query_db(
            proxy_repo,
            """
            SELECT jo.path
            FROM job_outputs jo
            JOIN artifacts a ON jo.artifact_id = a.id
            WHERE jo.job_id = ? AND a.source_type = 's3'
            """,
            (job_id,),
        )
        paths = [r["path"] for r in rows]
        assert "s3://fail-bucket/before_crash.csv" in paths


class TestProxyDaemonLifecycle:
    """Tests for the proxy daemon start/stop/status CLI commands."""

    def test_proxy_daemon_start_stop_lifecycle(
        self,
        temp_git_repo,
        roar_cli,
        proxy_binary,
    ):
        """roar proxy start → status → stop → status lifecycle works."""
        # Start daemon
        start_result = roar_cli("proxy", "start", check=False)
        assert start_result.returncode == 0, (
            f"proxy start failed:\nstdout={start_result.stdout}\nstderr={start_result.stderr}"
        )
        assert "pid=" in start_result.stdout
        assert "port=" in start_result.stdout

        # Status shows running
        status_result = roar_cli("proxy", "status", check=False)
        assert status_result.returncode == 0
        assert "running" in status_result.stdout.lower()

        # Stop daemon
        stop_result = roar_cli("proxy", "stop", check=False)
        assert stop_result.returncode == 0
        assert "stopped" in stop_result.stdout.lower()

        # Status shows not running
        status_result2 = roar_cli("proxy", "status", check=False)
        assert status_result2.returncode == 0
        assert "not running" in status_result2.stdout.lower()
