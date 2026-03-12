from __future__ import annotations

from pathlib import Path

import pytest

from roar.backends.ray.fragment_reconstituter import FragmentReconstituter
from tests.e2e.ray.conftest import (
    init_host_project,
    load_fragment_key,
    make_host_project_dir,
    query_roar_db,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def _run_basic_file_job(
    project_dir: Path,
    ray_cluster: dict[str, str],
) -> tuple[object, dict[str, str]]:
    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "basic_file_io.py",
        use_fragment_store=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result, load_fragment_key(project_dir)


def _count_rows(project_dir: Path) -> dict[str, int]:
    return {
        "jobs": int(
            query_roar_db(
                project_dir,
                "SELECT COUNT(*) AS count FROM jobs WHERE job_type = 'ray_task'",
            )[0]["count"]
        ),
        "artifacts": int(
            query_roar_db(project_dir, "SELECT COUNT(*) AS count FROM artifacts")[0]["count"]
        ),
        "job_inputs": int(
            query_roar_db(project_dir, "SELECT COUNT(*) AS count FROM job_inputs")[0]["count"]
        ),
        "job_outputs": int(
            query_roar_db(project_dir, "SELECT COUNT(*) AS count FROM job_outputs")[0]["count"]
        ),
        "artifact_hashes": int(
            query_roar_db(project_dir, "SELECT COUNT(*) AS count FROM artifact_hashes")[0]["count"]
        ),
    }


def test_auto_reconstitution_populates_local_roar_db(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-reconst")
    init_host_project(project_dir)

    result, _key_payload = _run_basic_file_job(project_dir, ray_cluster)
    counts = _count_rows(project_dir)

    assert "[roar] lineage reconstituted:" in f"{result.stdout}\n{result.stderr}"
    assert counts["jobs"] > 0
    assert counts["artifacts"] > 0
    assert counts["job_inputs"] > 0
    assert counts["job_outputs"] > 0


def test_reconstituted_artifact_hash_rows_are_present_and_well_formed(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-reconst")
    init_host_project(project_dir)

    _run_basic_file_job(project_dir, ray_cluster)
    rows = query_roar_db(
        project_dir,
        """
        SELECT ah.algorithm, ah.digest, a.path
        FROM artifact_hashes ah
        JOIN artifacts a ON a.id = ah.artifact_id
        ORDER BY a.path
        """,
    )

    assert rows, "Expected artifact_hashes rows to be created during reconstitution"
    for row in rows:
        digest = str(row["digest"] or "")
        assert row["algorithm"]
        assert row["path"]
        assert digest
        int(digest, 16)


def test_reconstitution_is_idempotent(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-reconst")
    init_host_project(project_dir)

    _result, key_payload = _run_basic_file_job(project_dir, ray_cluster)
    before = _count_rows(project_dir)
    db_path = project_dir / ".roar" / "roar.db"

    second = FragmentReconstituter(
        session_id=key_payload["session_id"],
        token=key_payload["token"],
        glaas_url="http://localhost:3001",
        roar_db_path=db_path,
    ).reconstitute()
    after = _count_rows(project_dir)

    assert second.jobs_merged == 0
    assert second.artifacts_merged == 0
    assert before == after
