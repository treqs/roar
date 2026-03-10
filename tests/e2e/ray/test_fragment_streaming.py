from __future__ import annotations

import pytest

from tests.e2e.ray.conftest import (
    decrypt_fragment_batches,
    fetch_fragment_batches,
    init_host_project,
    load_fragment_key,
    make_host_project_dir,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(180)]


def _fragment_paths(fragments: list[dict[str, object]]) -> set[str]:
    paths: set[str] = set()
    for fragment in fragments:
        for key in ("reads", "writes"):
            refs = fragment.get(key, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    paths.add(ref["path"])
    return paths


def test_file_io_job_streams_encrypted_fragments_to_glaas(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-stream")
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

    assert batches, "Expected at least one streamed fragment batch"
    assert all(isinstance(batch.get("encrypted_batch"), str) for batch in batches)


def test_fragment_batches_are_opaque_ciphertext(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-stream")
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

    expected_markers = ("basic_file_io/input.json", "basic_file_io/output.json")
    for batch in batches:
        encrypted_batch = str(batch.get("encrypted_batch") or "")
        assert encrypted_batch
        assert not any(marker in encrypted_batch for marker in expected_markers)


def test_decrypted_fragments_include_worker_file_reads_and_writes(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("fragment-stream")
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
    fragments = decrypt_fragment_batches(batches, key_payload["token"])
    paths = _fragment_paths(fragments)

    assert any(path.endswith("/artifacts/basic_file_io/input.json") for path in paths)
    assert any(path.endswith("/artifacts/basic_file_io/output.json") for path in paths)
