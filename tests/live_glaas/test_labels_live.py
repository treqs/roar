"""
Live GLaaS tests for label synchronization semantics.
"""

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tests.live_glaas import test_composite_live as composite_live

pytest_plugins = ("tests.live_glaas.test_composite_live",)
pytestmark = pytest.mark.live_glaas


def _clear_remote_label_storage() -> None:
    rows = composite_live._db_query_rows(
        """
        SELECT
          to_regclass('public.current_label_values') IS NOT NULL AS has_current_label_values,
          to_regclass('public.label_versions') IS NOT NULL AS has_label_versions,
          to_regclass('public.label_targets') IS NOT NULL AS has_label_targets,
          to_regclass('public.label_keys') IS NOT NULL AS has_label_keys,
          to_regclass('public.label_facts') IS NOT NULL AS has_label_facts,
          to_regclass('public.labels') IS NOT NULL AS has_labels
        """
    )
    assert rows, "Expected label storage probe row"
    row = rows[0]

    current_tables = [
        table_name
        for table_name, present in (
            ("current_label_values", row.get("has_current_label_values")),
            ("label_versions", row.get("has_label_versions")),
            ("label_targets", row.get("has_label_targets")),
            ("label_keys", row.get("has_label_keys")),
        )
        if present
    ]
    if current_tables:
        current_table_list = ", ".join(f'"{table}"' for table in current_tables)
        composite_live._db_query_rows(
            f"TRUNCATE TABLE {current_table_list} RESTART IDENTITY CASCADE"
        )

    legacy_tables = [
        table_name
        for table_name, present in (
            ("label_facts", row.get("has_label_facts")),
            ("labels", row.get("has_labels")),
        )
        if present
    ]
    if legacy_tables:
        legacy_table_list = ", ".join(f'"{table}"' for table in legacy_tables)
        composite_live._db_query_rows(
            f"TRUNCATE TABLE {legacy_table_list} RESTART IDENTITY CASCADE"
        )


@pytest.fixture(autouse=True)
def _clear_remote_labels(_serialize_external_label_tests):
    del _serialize_external_label_tests
    rows = composite_live._db_query_rows("SELECT 1 AS ok")
    assert rows and str(rows[0].get("ok")) == "1", f"Unexpected GLaaS database probe result: {rows}"
    _clear_remote_label_storage()


@pytest.fixture(autouse=True)
def _serialize_external_label_tests():
    external_glaas_url = os.environ.get("GLAAS_URL")
    if not external_glaas_url:
        yield
        return

    lock_path = Path(tempfile.gettempdir()) / "roar-live-glaas-label-tests.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _assert_ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    return result.stdout


def _run_roar(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _prepare_processed_artifact(repo: Path) -> dict[str, object]:
    (repo / "input.csv").write_text("id,value\n1,foo\n2,bar\n", encoding="utf-8")
    (repo / "preprocess.py").write_text(
        """
import sys

with open(sys.argv[1], "r", encoding="utf-8") as src:
    payload = src.read().upper()

with open(sys.argv[2], "w", encoding="utf-8") as dst:
    dst.write(payload)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _commit_all(repo, "Add preprocess inputs")

    _assert_ok(
        _run_roar(repo, "run", sys.executable, "preprocess.py", "input.csv", "processed.csv")
    )
    _commit_all(repo, "Add preprocess output")

    lineage = json.loads(_assert_ok(_run_roar(repo, "lineage", "processed.csv")))
    return {
        "artifact_hash": lineage["artifact"]["hash"],
        "session_hash": _computed_remote_session_hash(repo),
        "step_number": composite_live._latest_step_number(repo),
    }


def _prepare_model_artifact(repo: Path) -> dict[str, object]:
    (repo / "train.py").write_text(
        """
with open("model.pt", "wb") as model_file:
    model_file.write(b"fake model weights" * 64)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _commit_all(repo, "Add training script")

    _assert_ok(_run_roar(repo, "run", sys.executable, "train.py"))
    _commit_all(repo, "Add training output")

    lineage = json.loads(_assert_ok(_run_roar(repo, "lineage", "model.pt")))
    return {"artifact_hash": lineage["artifact"]["hash"]}


def _computed_remote_session_hash(repo: Path) -> str:
    local_session_hash = composite_live._get_active_session_hash(repo)
    with sqlite3.connect(repo / ".roar" / "roar.db") as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE hash = ?",
            (local_session_hash,),
        ).fetchone()
    assert row is not None, f"Active local session not found for hash {local_session_hash}"
    status_result = _run_roar(repo, "status")
    assert status_result.returncode == 0, status_result.stderr or status_result.stdout
    match = re.search(r"DAG hash:\s+([a-f0-9]{64})", status_result.stdout)
    assert match is not None, status_result.stdout
    return match.group(1)


def _local_job_uid(repo: Path, step_number: int) -> str:
    with sqlite3.connect(repo / ".roar" / "roar.db") as conn:
        session_row = conn.execute(
            "SELECT id FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert session_row is not None, "No active local session found"
        job_row = conn.execute(
            """
            SELECT job_uid
            FROM jobs
            WHERE session_id = ? AND step_number = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(session_row[0]), step_number),
        ).fetchone()
    assert job_row is not None and job_row[0], f"No local job uid found for step @{step_number}"
    return str(job_row[0])


def _label_api_get(glaas_url: str, path: str, **query: str) -> dict[str, object]:
    query_string = urlencode(query)
    api_path = f"{path}?{query_string}" if query_string else path
    payload = composite_live._api_get(glaas_url, api_path)
    assert payload["success"] is True, payload
    data = payload.get("data")
    assert isinstance(data, dict), payload
    return data


def _label_history_rows(
    glaas_url: str,
    *,
    entity_type: str,
    session_hash: str | None = None,
    job_uid: str | None = None,
    artifact_hash: str | None = None,
) -> list[tuple[int, dict[str, object]]]:
    query: dict[str, str] = {"entity_type": entity_type}
    if session_hash:
        query["session_hash"] = session_hash
    if job_uid:
        query["job_uid"] = job_uid
    if artifact_hash:
        query["artifact_hash"] = artifact_hash
    try:
        history = _label_api_get(
            glaas_url,
            "/api/v1/labels/history",
            **query,
        )
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return []
        raise
    labels = history.get("labels")
    assert isinstance(labels, list), history
    return [
        (int(label["version"]), label["metadata"])
        for label in labels
        if isinstance(label, dict) and isinstance(label.get("metadata"), dict)
    ]


def _remote_artifact_label_rows(
    glaas_url: str, artifact_hash: str
) -> list[tuple[int, dict[str, object]]]:
    return _label_history_rows(
        glaas_url,
        entity_type="artifact",
        artifact_hash=artifact_hash,
    )


def _remote_job_label_rows(
    glaas_url: str, session_hash: str, job_uid: str
) -> list[tuple[int, dict[str, object]]]:
    return _label_history_rows(
        glaas_url,
        entity_type="job",
        session_hash=session_hash,
        job_uid=job_uid,
    )


def _remote_session_label_rows(
    glaas_url: str, session_hash: str
) -> list[tuple[int, dict[str, object]]]:
    return _label_history_rows(
        glaas_url,
        entity_type="dag",
        session_hash=session_hash,
    )


def _assert_synced_run_job_label_metadata(
    metadata: dict[str, object],
    *,
    phase: str,
) -> None:
    assert metadata.get("phase") == phase
    roar = metadata.get("roar")
    assert isinstance(roar, dict), metadata
    assert roar.get("schema_version") == 1
    operation = roar.get("operation")
    assert isinstance(operation, dict), roar
    assert operation.get("kind") == "run"


def test_register_syncs_current_local_labels_only_when_register_called(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
):
    repo = glaas_configured
    refs = _prepare_processed_artifact(repo)
    session_hash = str(refs["session_hash"])
    artifact_hash = str(refs["artifact_hash"])
    step_number = int(refs["step_number"])
    job_uid = _local_job_uid(repo, step_number)

    _assert_ok(
        _run_roar(
            repo,
            "label",
            "set",
            "dag",
            "current",
            "experiment=ablation-7",
            "project=forecasting",
        )
    )
    _assert_ok(_run_roar(repo, "label", "set", "job", f"@{step_number}", "phase=preprocess"))
    _assert_ok(
        _run_roar(repo, "label", "set", "artifact", "processed.csv", "owner=ml", "stage=gold")
    )

    assert _remote_session_label_rows(glaas_url, session_hash) == []
    assert _remote_job_label_rows(glaas_url, session_hash, job_uid) == []
    assert _remote_artifact_label_rows(glaas_url, artifact_hash) == []

    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    assert _remote_session_label_rows(glaas_url, session_hash) == [
        (1, {"experiment": "ablation-7", "project": "forecasting"})
    ]
    job_rows = _remote_job_label_rows(glaas_url, session_hash, job_uid)
    assert len(job_rows) == 1
    version, job_metadata = job_rows[0]
    assert version == 1
    _assert_synced_run_job_label_metadata(job_metadata, phase="preprocess")
    assert _remote_artifact_label_rows(glaas_url, artifact_hash) == [
        (1, {"owner": "ml", "stage": "gold"})
    ]


def test_register_after_local_label_change_creates_new_remote_version(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
):
    repo = glaas_configured
    refs = _prepare_processed_artifact(repo)
    artifact_hash = str(refs["artifact_hash"])

    _assert_ok(
        _run_roar(repo, "label", "set", "artifact", "processed.csv", "owner=ml", "stage=raw")
    )
    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    _assert_ok(_run_roar(repo, "label", "set", "artifact", "processed.csv", "stage=gold"))
    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    assert _remote_artifact_label_rows(glaas_url, artifact_hash) == [
        (1, {"owner": "ml", "stage": "raw"}),
        (2, {"owner": "ml", "stage": "gold"}),
    ]


def test_register_exposes_current_labels_via_label_api(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
):
    repo = glaas_configured
    refs = _prepare_processed_artifact(repo)
    session_hash = str(refs["session_hash"])
    artifact_hash = str(refs["artifact_hash"])
    step_number = int(refs["step_number"])
    job_uid = _local_job_uid(repo, step_number)

    _assert_ok(
        _run_roar(
            repo,
            "label",
            "set",
            "dag",
            "current",
            "experiment=ablation-7",
            "project=forecasting",
        )
    )
    _assert_ok(_run_roar(repo, "label", "set", "job", f"@{step_number}", "phase=preprocess"))
    _assert_ok(
        _run_roar(repo, "label", "set", "artifact", "processed.csv", "owner=ml", "stage=gold")
    )

    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    dag_label = _label_api_get(
        glaas_url,
        "/api/v1/labels/current",
        entity_type="dag",
        session_hash=session_hash,
    )
    assert dag_label == {
        "id": dag_label["id"],
        "entityType": "dag",
        "version": 1,
        "metadata": {"experiment": "ablation-7", "project": "forecasting"},
        "createdAt": dag_label["createdAt"],
        "sessionHash": session_hash,
    }

    job_label = _label_api_get(
        glaas_url,
        "/api/v1/labels/current",
        entity_type="job",
        session_hash=session_hash,
        job_uid=job_uid,
    )
    assert job_label == {
        "id": job_label["id"],
        "entityType": "job",
        "version": 1,
        "metadata": job_label["metadata"],
        "createdAt": job_label["createdAt"],
        "sessionHash": session_hash,
        "jobUid": job_uid,
    }
    assert isinstance(job_label["metadata"], dict)
    _assert_synced_run_job_label_metadata(job_label["metadata"], phase="preprocess")

    artifact_label = _label_api_get(
        glaas_url,
        "/api/v1/labels/current",
        entity_type="artifact",
        artifact_hash=artifact_hash,
    )
    assert artifact_label == {
        "id": artifact_label["id"],
        "entityType": "artifact",
        "version": 1,
        "metadata": {"owner": "ml", "stage": "gold"},
        "createdAt": artifact_label["createdAt"],
        "artifactHash": artifact_hash,
    }


def test_register_updates_are_visible_via_label_history_and_search_api(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
):
    repo = glaas_configured
    refs = _prepare_processed_artifact(repo)
    artifact_hash = str(refs["artifact_hash"])

    _assert_ok(
        _run_roar(
            repo,
            "label",
            "set",
            "artifact",
            "processed.csv",
            "owner=ml",
            "stage=api-history-v1",
        )
    )
    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    _assert_ok(_run_roar(repo, "label", "set", "artifact", "processed.csv", "stage=api-history-v2"))
    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    history = _label_api_get(
        glaas_url,
        "/api/v1/labels/history",
        entity_type="artifact",
        artifact_hash=artifact_hash,
    )
    labels = history.get("labels")
    assert isinstance(labels, list)
    assert labels == [
        {
            "id": labels[0]["id"],
            "entityType": "artifact",
            "version": 1,
            "metadata": {"owner": "ml", "stage": "api-history-v1"},
            "createdAt": labels[0]["createdAt"],
            "artifactHash": artifact_hash,
        },
        {
            "id": labels[1]["id"],
            "entityType": "artifact",
            "version": 2,
            "metadata": {"owner": "ml", "stage": "api-history-v2"},
            "createdAt": labels[1]["createdAt"],
            "artifactHash": artifact_hash,
        },
    ]

    current_search = _label_api_get(
        glaas_url,
        "/api/v1/labels/search",
        entity_type="artifact",
        key="stage",
        value="api-history-v2",
    )
    assert current_search == {
        "matches": [
            {
                "id": current_search["matches"][0]["id"],
                "entityType": "artifact",
                "version": 2,
                "metadata": {"owner": "ml", "stage": "api-history-v2"},
                "createdAt": current_search["matches"][0]["createdAt"],
                "artifactHash": artifact_hash,
            }
        ],
        "total": 1,
    }

    stale_search = _label_api_get(
        glaas_url,
        "/api/v1/labels/search",
        entity_type="artifact",
        key="stage",
        value="api-history-v1",
    )
    assert stale_search == {"matches": [], "total": 0}


def test_put_syncs_current_artifact_labels_for_tracked_file(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
    skip_upload_env,
):
    repo = glaas_configured
    refs = _prepare_model_artifact(repo)
    artifact_hash = str(refs["artifact_hash"])

    _assert_ok(
        _run_roar(repo, "label", "set", "artifact", "model.pt", "owner=ml", "stage=candidate")
    )

    assert _remote_artifact_label_rows(glaas_url, artifact_hash) == []

    _assert_ok(
        _run_roar(
            repo,
            "put",
            "model.pt",
            "s3://test-bucket/models/model.pt",
            "-m",
            "publish labeled model",
            "--no-tag",
        )
    )

    assert _remote_artifact_label_rows(glaas_url, artifact_hash) == [
        (1, {"owner": "ml", "stage": "candidate"})
    ]


def test_register_syncs_copied_labels_for_new_artifact_version_only_after_register(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
):
    repo = glaas_configured
    refs = _prepare_processed_artifact(repo)
    source_hash = str(refs["artifact_hash"])
    _assert_ok(
        _run_roar(
            repo,
            "label",
            "set",
            "artifact",
            source_hash,
            "owner=ml",
            "stage=baseline",
        )
    )

    (repo / "input.csv").write_text("id,value\n1,baz\n2,qux\n3,quux\n", encoding="utf-8")
    _commit_all(repo, "Update input.csv for processed v2")

    _assert_ok(
        _run_roar(repo, "run", sys.executable, "preprocess.py", "input.csv", "processed.csv")
    )
    _commit_all(repo, "Track processed.csv v2")

    destination_hash = json.loads(_assert_ok(_run_roar(repo, "lineage", "processed.csv")))[
        "artifact"
    ]["hash"]
    assert destination_hash != source_hash

    _assert_ok(
        _run_roar(
            repo,
            "label",
            "set",
            "artifact",
            "processed.csv",
            "note=current",
            "stage=edited",
        )
    )
    _assert_ok(
        _run_roar(
            repo,
            "label",
            "cp",
            "artifact",
            source_hash,
            "artifact",
            "processed.csv",
        )
    )

    assert _remote_artifact_label_rows(glaas_url, source_hash) == []
    assert _remote_artifact_label_rows(glaas_url, destination_hash) == []

    _assert_ok(_run_roar(repo, "register", "processed.csv"))

    assert _remote_artifact_label_rows(glaas_url, source_hash) == []
    assert _remote_artifact_label_rows(glaas_url, destination_hash) == [
        (1, {"note": "current", "owner": "ml", "stage": "baseline"})
    ]
