"""
TDD: roar captures Ray Data file I/O from internal worker tasks.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.ray.conftest import submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


class TestRayDataCapture:
    def test_read_csv_and_write_parquet_are_captured(self, ray_cluster):
        stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/ray_data_pipeline.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"

        input_rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT ji.path FROM job_inputs ji WHERE ji.path LIKE '%ray_data_input.csv'",
        )
        assert input_rows, (
            "Expected Ray Data read_csv input file to appear in job_inputs, "
            "but no matching artifact was captured."
        )

        output_rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT jo.path FROM job_outputs jo WHERE jo.path LIKE '%ray_data_output%'",
        )
        assert output_rows, (
            "Expected Ray Data parquet output to appear in job_outputs, "
            "but no output path under ray_data_output was captured."
        )
