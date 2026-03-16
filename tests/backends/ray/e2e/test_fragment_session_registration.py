from __future__ import annotations

import uuid

import pytest

from tests.backends.ray.e2e.conftest import (
    fetch_fragment_batches,
    init_host_project,
    load_fragment_key,
    make_host_project_dir,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def test_roar_ray_submit_creates_fragment_key_file(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-session")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "basic_file_io.py",
        use_fragment_store=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    key_payload = load_fragment_key(project_dir)

    uuid.UUID(key_payload["session_id"])
    assert len(key_payload["token"]) == 64
    assert len(key_payload["token_hash"]) == 64


def test_fragment_session_is_preregistered_in_glaas(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-session")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "basic_file_io.py",
        use_fragment_store=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    assert isinstance(batches, list)
