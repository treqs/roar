"""
TDD: roar captures Ray Data file I/O from internal worker tasks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.ray.conftest import (
    query_roar_db_on_head,
    reset_roar_project_on_head,
    run_roar_ray_job_on_head,
)

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"
pytestmark = [pytest.mark.e2e, pytest.mark.ray_diagnostic, pytest.mark.timeout(180)]


@pytest.fixture(autouse=True)
def reset_roar_state(ray_cluster):
    del ray_cluster
    reset_roar_project_on_head(COMPOSE_FILE)
    yield


class TestRayDataCapture:
    def test_read_csv_and_write_parquet_are_captured(self, ray_cluster):
        stdout, stderr, returncode = run_roar_ray_job_on_head(
            f"{JOBS_DIR}/ray_data_pipeline.py",
            compose_file=COMPOSE_FILE,
            use_fragment_store=True,
        )
        assert returncode == 0, f"Job failed:\n{stderr}\n{stdout}"

        input_rows = query_roar_db_on_head(
            "SELECT ji.path FROM job_inputs ji WHERE ji.path LIKE '%ray_data_input.csv'",
        )
        assert input_rows, (
            "Expected Ray Data read_csv input file to appear in job_inputs, "
            "but no matching artifact was captured."
        )

        output_rows = query_roar_db_on_head(
            "SELECT jo.path FROM job_outputs jo WHERE jo.path LIKE '%ray_data_output%'",
        )
        assert output_rows, (
            "Expected Ray Data parquet output to appear in job_outputs, "
            "but no output path under ray_data_output was captured."
        )
