from __future__ import annotations

import pytest

from tests.backends.ray.e2e.conftest import (
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


def test_host_submit_streams_fragments_with_glaas_url_only(
    ray_cluster: dict[str, str],
) -> None:
    project_dir = make_host_project_dir("glaas-url-only")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "basic_file_io.py",
        use_fragment_store=True,
        extra_env={"GLAAS_API_URL": ""},
    )

    assert result.returncode == 0, result.stderr or result.stdout
    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    assert batches, "Expected streamed fragment batches when only GLAAS_URL is configured"

    fragments = decrypt_fragment_batches(batches, key_payload["token"])
    paths = _fragment_paths(fragments)
    assert any(path.endswith("/artifacts/basic_file_io/input.json") for path in paths)
    assert any(path.endswith("/artifacts/basic_file_io/output.json") for path in paths)
