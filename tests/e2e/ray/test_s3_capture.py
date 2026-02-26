"""
TDD: roar captures S3 I/O from Ray workers via the proxy.

These tests define the target behaviour for S3 proxy injection into Ray workers.
They FAIL until roar propagates AWS_ENDPOINT_URL to workers and collects proxy logs.

Run against a live cluster:
    pytest tests/e2e/ray/test_s3_capture.py -v --timeout=120
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.ray.conftest import submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


class TestS3Capture:
    """roar captures S3 operations performed by Ray workers."""

    def test_worker_s3_put_appears_as_output_artifact(self, ray_cluster):
        """
        S3 PutObject calls made by Ray workers should be captured
        as output artifacts via roar's S3 proxy.

        FAILS until roar propagates AWS_ENDPOINT_URL into Ray workers
        and collects proxy logs from each node.
        """
        stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/s3_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT path, source_type FROM artifacts WHERE source_type IN ('s3', 'proxy')",
        )
        assert len(rows) >= 1, (
            "Expected an S3 artifact from the Ray worker's boto3 put_object call, "
            "but none were captured. "
            "roar is not yet routing Ray worker S3 traffic through the proxy."
        )

    def test_worker_s3_get_appears_as_input_artifact(self, ray_cluster):
        """
        S3 GetObject calls from Ray workers should appear as input artifacts.

        FAILS until roar's proxy captures worker S3 traffic.
        """
        stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/s3_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT a.path FROM job_inputs ji "
            "JOIN artifacts a ON ji.artifact_id = a.id "
            "WHERE a.source_type IN ('s3', 'proxy')",
        )
        assert len(rows) >= 1, (
            "Expected S3 GetObject from Ray worker to appear as a job input, "
            "but none were captured."
        )

    def test_s3_artifact_has_etag(self, ray_cluster):
        """
        S3 artifacts captured via the proxy should include their ETag
        (content hash) for content-based deduplication.

        FAILS until roar's proxy collects and records ETags from worker traffic.
        """
        submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/s3_io.py",
            env={"ROAR_WRAP": "1"},
        )

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT hash FROM artifacts WHERE source_type IN ('s3', 'proxy') AND hash IS NOT NULL",
        )
        assert len(rows) >= 1, (
            "Expected S3 artifact to have a hash (ETag) from proxy capture, "
            "but no hashed S3 artifacts were found."
        )

    def test_worker_s3_write_artifact_has_nonzero_size(self, ray_cluster):
        """S3 write artifacts should persist non-zero size for non-empty object bodies."""
        submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/s3_io.py",
            env={"ROAR_WRAP": "1"},
        )

        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT size FROM artifacts "
            "WHERE source_type IN ('s3', 'proxy') AND path LIKE 's3://%' "
            "ORDER BY first_seen_at DESC",
        )
        assert len(rows) >= 1, "Expected at least one captured S3 artifact."
        assert any(int(row["size"]) > 0 for row in rows), (
            "Expected at least one captured S3 write artifact with size > 0, "
            "but all captured S3 artifact sizes were 0."
        )
