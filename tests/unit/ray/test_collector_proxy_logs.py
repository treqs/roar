from __future__ import annotations

import sqlite3
from pathlib import Path

from roar.db.schema import SCHEMA
from roar.ray import collector
from roar.ray.fragment import ArtifactRef, TaskFragment


def _init_db(project_dir: Path) -> Path:
    db_path = project_dir / ".roar" / "roar.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def test_merge_proxy_logs_parses_s3_lines_into_events() -> None:
    task_events: dict[str, list[dict[str, object]]] = {}
    proxy_logs = {
        "node-abc": {
            "node_id": "node-abc",
            "proxy_log_lines": [
                "ROAR_PROXY_READY port=12345",
                "[S3:GetObject] s3://bucket/input.csv  etag=etag-in",
                "[S3:PutObject] s3://bucket/output.csv  etag=etag-out",
            ],
        }
    }

    collector._merge_proxy_logs(task_events, proxy_logs)

    assert set(task_events) == {"proxy-node-abc"}
    events = task_events["proxy-node-abc"]
    assert len(events) == 2

    read_event, write_event = events
    assert read_event["path"] == "s3://bucket/input.csv"
    assert read_event["mode"] == "r"
    assert read_event["capture_method"] == "proxy"
    assert read_event["hash"] == "etag-in"
    assert read_event["hash_algorithm"] == "etag"

    assert write_event["path"] == "s3://bucket/output.csv"
    assert write_event["mode"] == "w"
    assert write_event["capture_method"] == "proxy"
    assert write_event["hash"] == "etag-out"
    assert write_event["hash_algorithm"] == "etag"


def test_collect_records_hash_rows_from_fragment_events(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    db_path = _init_db(project_dir)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fragment = TaskFragment(
        job_uid="abcd1234",
        parent_job_uid="driver01",
        ray_task_id="task-abcd1234",
        ray_worker_id="worker-1",
        ray_node_id="node-1",
        ray_actor_id=None,
        function_name="eval_model",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        reads=[],
        writes=[
            ArtifactRef(
                path="s3://output-bucket/results/run-1/final_report.json",
                hash="etag-final-123",
                hash_algorithm="etag",
                size=123,
                capture_method="proxy",
            )
        ],
    )

    monkeypatch.setattr(collector, "_collect_actor_payload", lambda: ([], [fragment.to_dict()]))

    collector.collect(project_dir=str(project_dir), log_dir=str(log_dir))

    conn = sqlite3.connect(db_path)
    hash_row = conn.execute(
        """
        SELECT ah.algorithm, ah.digest
        FROM artifacts a
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE a.first_seen_path = ?
        """,
        ("s3://output-bucket/results/run-1/final_report.json",),
    ).fetchone()
    output_row = conn.execute(
        """
        SELECT jo.path
        FROM job_outputs jo
        JOIN artifacts a ON a.id = jo.artifact_id
        WHERE a.first_seen_path = ?
        """,
        ("s3://output-bucket/results/run-1/final_report.json",),
    ).fetchone()
    conn.close()

    assert hash_row == ("etag", "etag-final-123")
    assert output_row == ("s3://output-bucket/results/run-1/final_report.json",)
