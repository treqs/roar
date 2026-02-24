"""Unit tests for DAG command data shaping."""

from __future__ import annotations

from types import SimpleNamespace

from roar.presenters.dag_data_builder import DagDataBuilder


def test_get_dag_data_excludes_composites_from_node_io_metrics() -> None:
    steps = [
        {
            "id": 1,
            "step_number": 1,
            "timestamp": 1.0,
            "command": "python train.py",
            "job_uid": "abc12345",
            "exit_code": 0,
            "job_type": None,
        }
    ]

    outputs = [
        {
            "path": "/repo/output/model.pt",
            "artifact_id": "art-primitive",
            "kind": "primitive",
            "hashes": [{"algorithm": "blake3", "digest": "a" * 64}],
        },
        {
            "path": "/repo/output/dataset",
            "artifact_id": "art-composite",
            "kind": "composite",
            "hashes": [{"algorithm": "composite-blake3", "digest": "b" * 64}],
        },
    ]
    inputs = [
        {
            "path": "/repo/input/train.csv",
            "artifact_id": "in-primitive",
            "kind": "primitive",
            "hashes": [{"algorithm": "blake3", "digest": "c" * 64}],
        },
        {
            "path": "/repo/input/dataset",
            "artifact_id": "in-composite",
            "kind": "composite",
            "hashes": [{"algorithm": "composite-blake3", "digest": "d" * 64}],
        },
    ]

    db_ctx = SimpleNamespace(
        sessions=SimpleNamespace(get_steps=lambda _sid: steps),
        session_service=SimpleNamespace(get_stale_steps=lambda _sid: []),
        jobs=SimpleNamespace(
            get_outputs=lambda _job_id: outputs,
            get_inputs=lambda _job_id: inputs,
        ),
    )

    builder = DagDataBuilder(db_ctx=db_ctx, session_id=1)
    dag = builder.build()

    assert len(dag["nodes"]) == 1
    metrics = dag["nodes"][0]["metrics"]
    assert metrics["inputs"] == 1
    assert metrics["outputs"] == 1
