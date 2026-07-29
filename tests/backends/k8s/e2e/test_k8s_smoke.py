"""Tier-1 smoke: single-pod k8s Job lineage captured and streamed to GLaaS.

Phase-0 product path (pre-backend): a roar-unaware training script runs in
a vanilla pod under `roar run`, the recorded job is exported as an
execution fragment, streamed to the local glaas-api through the shared
fragment-session pipeline, then fetched, decrypted, and merged into a
local roar db on the host — the same loop the future `roar k8s` backend
will own end to end.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from .conftest import NAMESPACE

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    # The module fixture runs a full Job (image pull + wheel install + trace)
    # inside the first test's budget; the default 60s gate timeout is too low.
    pytest.mark.timeout(600),
]

SMOKE_TASK_NAME = "k8s-smoke-train"


def _smoke_fragment(smoke_run: dict[str, Any]) -> dict[str, Any]:
    fragments = [
        fragment
        for fragment in smoke_run["fragments"]
        if fragment.get("task_name") == SMOKE_TASK_NAME
    ]
    assert fragments, (
        f"No {SMOKE_TASK_NAME} fragment found in "
        f"{len(smoke_run['fragments'])} decrypted fragment(s).\n"
        f"pod logs:\n{smoke_run['logs']}"
    )
    return fragments[-1]


def _refs_by_suffix(refs: list[dict[str, Any]], suffix: str) -> dict[str, Any] | None:
    for ref in refs:
        if str(ref.get("path", "")).endswith(suffix):
            return ref
    return None


def test_job_streams_traced_lineage_fragment(smoke_run: dict[str, Any]) -> None:
    fragment = _smoke_fragment(smoke_run)

    assert fragment.get("backend") == "k8s"
    assert fragment.get("exit_code") == 0

    reads = [ref for ref in fragment.get("reads", []) if isinstance(ref, dict)]
    writes = [ref for ref in fragment.get("writes", []) if isinstance(ref, dict)]

    dataset = _refs_by_suffix(reads, "dataset.csv")
    assert dataset is not None, f"dataset.csv not in reads: {reads}\nlogs:\n{smoke_run['logs']}"
    assert dataset.get("hash"), f"dataset.csv read has no hash: {dataset}"

    for expected in ("model.bin", "metrics.json"):
        ref = _refs_by_suffix(writes, expected)
        assert ref is not None, f"{expected} not in writes: {writes}\nlogs:\n{smoke_run['logs']}"
        assert ref.get("hash"), f"{expected} write has no hash: {ref}"
        assert int(ref.get("size") or 0) > 0, f"{expected} write has no size: {ref}"


def test_fragment_carries_k8s_identity_metadata(smoke_run: dict[str, Any]) -> None:
    fragment = _smoke_fragment(smoke_run)
    metadata = fragment.get("backend_metadata") or {}

    pod_uid = metadata.get("k8s_pod_uid")
    assert pod_uid, f"missing k8s_pod_uid in backend_metadata: {metadata}"
    assert metadata.get("k8s_namespace") == NAMESPACE
    assert metadata.get("k8s_pod_name", "").startswith("roar-smoke-")
    assert metadata.get("k8s_node_name")

    task_id = str(fragment.get("task_id") or "")
    assert task_id == f"{pod_uid}:trainer:0:0", (
        f"task_id does not follow the pod-uid:container:index:attempt contract: {task_id}"
    )


def test_fragments_reconstitute_into_local_db(smoke_run: dict[str, Any], tmp_path: Path) -> None:
    from roar.db.context import create_database_context
    from roar.execution.fragments.lineage import (
        FragmentLineageBackend,
        merge_execution_fragments,
    )
    from roar.execution.fragments.models import (
        ExecutionFragment,
        derive_fragment_identity,
    )

    k8s_test_lineage_backend = FragmentLineageBackend(
        job_type="k8s_task",
        command_for_fragment=lambda fragment: f"k8s_task:{fragment.task_name}",
        script_for_fragment=lambda fragment: fragment.task_name or None,
        execution_role_from_fragment=lambda fragment, _parent: "task",
        metadata_from_fragment=lambda fragment, _parent: {
            "k8s_task_id": fragment.task_id,
            **(fragment.backend_metadata or {}),
        },
        task_identity_from_metadata=lambda parent_job_uid, job_uid, metadata: (
            derive_fragment_identity(
                "k8s",
                parent_job_uid,
                str(metadata.get("k8s_task_id") or ""),
                job_uid,
            )
        ),
    )

    project_dir = tmp_path / "project"
    roar_dir = project_dir / ".roar"
    roar_dir.mkdir(parents=True)
    db_path = roar_dir / "roar.db"
    with create_database_context(roar_dir):
        pass  # connect() initializes the schema

    fragments = [
        ExecutionFragment.from_dict(fragment)
        for fragment in smoke_run["fragments"]
        if fragment.get("task_name") == SMOKE_TASK_NAME
    ]
    assert fragments, "no smoke fragments available for reconstitution"

    merge_execution_fragments(
        fragments=fragments,
        project_dir=str(project_dir),
        backend=k8s_test_lineage_backend,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        jobs = conn.execute("SELECT * FROM jobs WHERE job_type = 'k8s_task'").fetchall()
        assert len(jobs) == 1, f"expected one merged k8s_task job, got {len(jobs)}"

        artifact_paths = {
            str(row["path"])
            for row in conn.execute(
                "SELECT path FROM job_outputs WHERE job_id = ?",
                (jobs[0]["id"],),
            ).fetchall()
        }
        assert any(path.endswith("model.bin") for path in artifact_paths), artifact_paths
        assert any(path.endswith("metrics.json") for path in artifact_paths), artifact_paths
    finally:
        conn.close()
