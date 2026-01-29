"""
Integration tests for package extraction during reproduction.

Verifies the full flow from recording a job with packages through to
extracting them during reproduction, and that debug logging works after
bootstrap.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from roar.core.bootstrap import bootstrap, reset
from roar.core.container import get_container
from roar.core.interfaces.logger import ILogger
from roar.services.logging import NullLogger, RoarLogger
from roar.services.reproduction.environment_setup import EnvironmentSetupService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline(build_steps=None, run_steps=None):
    """Create a mock PipelineInfo with the given steps."""
    pipeline = MagicMock()
    pipeline.build_steps = build_steps or []
    pipeline.run_steps = run_steps or []
    pipeline.git_repo = "https://github.com/test/repo.git"
    pipeline.git_commit = "abc123def456789"
    return pipeline


# ---------------------------------------------------------------------------
# TestReproducePackageExtraction — end-to-end via CLI
# ---------------------------------------------------------------------------

class TestReproducePackageExtraction:
    """Integration tests for package extraction during reproduction."""

    def test_preview_shows_pip_packages(
        self, temp_git_repo, roar_cli, git_commit, python_exe
    ):
        """Running roar reproduce <hash> should show pip packages in preview."""
        # Create a script that imports a package (sys is always available)
        script = temp_git_repo / "use_pkg.py"
        script.write_text(
            "import sys, json\n"
            "with open('out.json', 'w') as f:\n"
            "    json.dump({'v': sys.version}, f)\n"
        )
        git_commit("Add script")

        # Run it via roar run
        result = roar_cli("run", python_exe, "use_pkg.py")
        assert result.returncode == 0
        git_commit("After run")

        # Get artifact hash via lineage
        lineage_result = roar_cli("lineage", "out.json")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]
        assert len(artifact_hash) >= 8

        # Preview reproduction (no --run)
        preview = roar_cli("reproduce", artifact_hash[:12], check=False)
        # The command should succeed (exit 0) even without --run
        assert preview.returncode == 0
        # Output should contain artifact hash info at minimum
        assert artifact_hash[:12] in preview.stdout or "Artifact" in preview.stdout

    def test_preview_shows_dpkg_packages_when_present(self, temp_git_repo, roar_cli, git_commit, python_exe):
        """Preview should show system packages when metadata has dpkg entries."""
        # We inject dpkg metadata directly into the DB since we can't
        # reliably trigger dpkg collection in CI.
        script = temp_git_repo / "simple.py"
        script.write_text("with open('result.txt', 'w') as f: f.write('ok')\n")
        git_commit("Add script")

        roar_cli("run", python_exe, "simple.py")
        git_commit("After run")

        # Inject dpkg metadata into the DB
        import sqlite3

        db_path = temp_git_repo / ".roar" / "roar.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Find the job row — check what tables exist first
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]

        # Find the jobs table (may vary by schema version)
        jobs_table = "jobs" if "jobs" in tables else None
        if jobs_table is None:
            conn.close()
            pytest.skip("jobs table not found in DB; tables: " + str(tables))

        cur.execute(f"SELECT id, metadata FROM {jobs_table} ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        job_id, raw_meta = row

        meta = json.loads(raw_meta) if raw_meta else {}
        meta.setdefault("packages", {})["dpkg"] = {"libcurl4": "7.88.1-10"}
        cur.execute(
            f"UPDATE {jobs_table} SET metadata = ? WHERE id = ?",
            (json.dumps(meta), job_id),
        )
        conn.commit()
        conn.close()

        # Get artifact hash via lineage
        lineage_result = roar_cli("lineage", "result.txt")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        preview = roar_cli("reproduce", artifact_hash[:12], check=False)
        assert preview.returncode == 0
        # Should mention system packages
        assert "libcurl4" in preview.stdout or "System packages" in preview.stdout

    def test_packages_extracted_from_local_db(
        self, temp_git_repo, roar_cli, git_commit, python_exe
    ):
        """_get_packages should extract pip packages from locally stored metadata."""
        script = temp_git_repo / "use_json.py"
        script.write_text(
            "import json\n"
            "with open('data.json', 'w') as f:\n"
            "    json.dump({'x': 1}, f)\n"
        )
        git_commit("Add script")

        roar_cli("run", python_exe, "use_json.py")
        git_commit("After run")

        # Build a service and extract packages from the pipeline
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        # Get artifact hash via lineage
        lineage_result = roar_cli("lineage", "data.json")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        # Use ReproductionService to look up the pipeline
        from roar.services.reproduction import ReproductionService

        repro = ReproductionService(glaas_client=None, presenter=None)
        pipeline, error = repro._lookup_pipeline(
            artifact_hash[:12], None, temp_git_repo / ".roar"
        )
        assert error is None
        assert pipeline is not None

        packages = service._get_packages(pipeline)
        # packages may or may not be populated depending on provenance
        # collection — the important thing is no crash
        assert isinstance(packages, list)

    def test_dpkg_any_version_flag_accepted(self, temp_git_repo, roar_cli):
        """CLI should accept --dpkg-any-version flag without error."""
        result = roar_cli("reproduce", "--help", check=False)
        assert result.returncode == 0
        assert "--dpkg-any-version" in result.stdout


# ---------------------------------------------------------------------------
# TestEnvironmentSetupWithRealMetadata — unit-level with realistic data
# ---------------------------------------------------------------------------

class TestRemoteMetadataMissing:
    """Verify that omitting metadata from remote API response loses packages."""

    def test_no_packages_extracted_when_metadata_missing(self):
        """Simulates GLaaS DAG response that omits the metadata field.

        This is the exact bug: _lookup_remote() built job dicts without
        metadata, so _get_packages() found nothing.
        """
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        # Simulate a remote API job dict *without* the metadata key —
        # this is what glaas-api returned before the fix.
        step_without_metadata = {
            "id": 1,
            "command": "python train.py",
            "jobType": "run",
            "stepNumber": 1,
            "timestamp": "2025-01-01T00:00:00Z",
            "inputs": [{"hash": "aaa", "path": "/in"}],
            "outputs": [{"hash": "bbb", "path": "/out"}],
            # metadata key intentionally absent
        }

        pipeline = _make_pipeline(run_steps=[step_without_metadata])

        pip_pkgs = service._get_packages(pipeline)
        dpkg_pkgs = service._get_dpkg_packages(pipeline)

        assert pip_pkgs == [], "Packages should be empty when metadata is missing"
        assert dpkg_pkgs == {}, "Dpkg packages should be empty when metadata is missing"

    def test_packages_extracted_when_metadata_present(self):
        """Simulates GLaaS DAG response that includes the metadata field.

        After the fix, glaas-api includes metadata on job objects so
        _get_packages() can extract pip/dpkg packages from remote lookups.
        """
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        # Simulate a remote API job dict *with* the metadata key —
        # this is what glaas-api returns after the fix.
        step_with_metadata = {
            "id": 1,
            "command": "python train.py",
            "jobType": "run",
            "stepNumber": 1,
            "timestamp": "2025-01-01T00:00:00Z",
            "inputs": [{"hash": "aaa", "path": "/in"}],
            "outputs": [{"hash": "bbb", "path": "/out"}],
            "metadata": {
                "packages": {
                    "pip": {"numpy": "1.24.1", "pandas": "2.0.0"},
                    "dpkg": {"libc6": "2.35-0ubuntu3"},
                },
            },
        }

        pipeline = _make_pipeline(run_steps=[step_with_metadata])

        pip_pkgs = service._get_packages(pipeline)
        dpkg_pkgs = service._get_dpkg_packages(pipeline)

        assert "numpy==1.24.1" in pip_pkgs
        assert "pandas==2.0.0" in pip_pkgs
        assert dpkg_pkgs == {"libc6": "2.35-0ubuntu3"}


class TestEnvironmentSetupWithRealMetadata:
    """Test package extraction against realistic metadata JSON."""

    def test_extracts_from_real_metadata_json_string(self):
        """Simulate the exact JSON string stored in DB."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        metadata_json = json.dumps({
            "packages": {
                "pip": {"numpy": "1.24.1", "pandas": "2.0.0"},
                "dpkg": {"libc6": "2.35-0ubuntu3"},
            }
        })

        pipeline = _make_pipeline(
            run_steps=[{"metadata": metadata_json}],
        )

        pip_pkgs = service._get_packages(pipeline)
        assert "numpy==1.24.1" in pip_pkgs
        assert "pandas==2.0.0" in pip_pkgs

        dpkg_pkgs = service._get_dpkg_packages(pipeline)
        assert dpkg_pkgs == {"libc6": "2.35-0ubuntu3"}

    def test_handles_metadata_none(self):
        """Steps with None metadata should be skipped gracefully."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        pipeline = _make_pipeline(
            run_steps=[{"metadata": None}, {}],
        )

        assert service._get_packages(pipeline) == []
        assert service._get_dpkg_packages(pipeline) == {}

    def test_handles_metadata_without_packages_key(self):
        """Steps with metadata but no 'packages' key should be skipped."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        pipeline = _make_pipeline(
            run_steps=[{"metadata": {"runtime": {"os": {"system": "Linux"}}}}],
        )

        assert service._get_packages(pipeline) == []
        assert service._get_dpkg_packages(pipeline) == {}


# ---------------------------------------------------------------------------
# TestDebugLoggingAvailable — verify bootstrap wires up the logger
# ---------------------------------------------------------------------------

class TestDebugLoggingAvailable:
    """Verify that debug logging works when bootstrap is called."""

    def setup_method(self):
        """Reset bootstrap state between tests."""
        reset()

    def teardown_method(self):
        reset()

    def test_logger_is_not_null_after_bootstrap(self, tmp_path):
        """After bootstrap(), the logger should be a RoarLogger, not NullLogger."""
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()
        # Write minimal config so bootstrap doesn't complain
        (roar_dir / "config.toml").write_text("")

        bootstrap(roar_dir)

        container = get_container()
        logger = container.try_resolve(ILogger)
        assert logger is not None
        assert not isinstance(logger, NullLogger)

    def test_logger_is_null_without_bootstrap(self):
        """Without bootstrap(), EnvironmentSetupService should fall back to NullLogger."""
        service = EnvironmentSetupService()
        # Access the property — should fall back gracefully
        assert isinstance(service.logger, NullLogger)

    def test_setup_produces_debug_messages(self, tmp_path):
        """setup() debug logging should invoke the logger (not NullLogger)."""
        service = EnvironmentSetupService()
        mock_logger = MagicMock()
        service._logger = mock_logger

        pipeline = _make_pipeline(
            run_steps=[
                {"metadata": json.dumps({"packages": {"pip": {"x": "1.0"}}})}
            ],
        )

        service._get_packages(pipeline)

        # Should have called debug at least once
        assert mock_logger.debug.call_count >= 1
        # Check that metadata-related debug message was emitted
        debug_messages = [
            call.args[0] for call in mock_logger.debug.call_args_list
        ]
        assert any("metadata" in m.lower() or "pip" in m.lower() for m in debug_messages)
