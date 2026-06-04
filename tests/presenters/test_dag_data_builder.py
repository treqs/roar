"""DagDataBuilder dependency edges, incl. composite-only producer→consumer."""

from __future__ import annotations

from unittest.mock import MagicMock

from roar.presenters.dag_data_builder import DagDataBuilder


def _builder(inputs_by_job, outputs_by_job) -> DagDataBuilder:
    db_ctx = MagicMock()
    db_ctx.jobs.get_inputs.side_effect = lambda jid: inputs_by_job.get(jid, [])
    db_ctx.jobs.get_outputs.side_effect = lambda jid: outputs_by_job.get(jid, [])
    b = DagDataBuilder(db_ctx, session_id=1)
    b._current_labels = lambda *a, **k: {}  # type: ignore[method-assign]
    return b


def test_composite_only_output_creates_dependency_edge() -> None:
    """A `roar get` whose sole output is a dataset composite must still link to
    the downstream step that consumes that composite (matched by artifact id,
    since the composite path is a logical hf://… URI, not a local file)."""
    composite = {
        "artifact_id": "comp-1",
        "kind": "composite",
        "component_count": 6543,
        "path": "hf://datasets/owner/name@abc",
        "hashes": [{"algorithm": "composite-sha256", "digest": "92fe"}],
    }
    shard = {
        "artifact_id": "shard-1",
        "kind": "primitive",
        "path": "/data/shard_00000.parquet",
        "hashes": [{"algorithm": "blake3", "digest": "aaaa"}],
    }
    step1 = {"id": 1, "step_number": 1, "timestamp": 1.0, "command": "roar get hf://…", "job_type": "get"}
    step2 = {"id": 2, "step_number": 2, "timestamp": 2.0, "command": "b3sum *", "job_type": None}
    steps_by_number = {1: [step1], 2: [step2]}

    builder = _builder(
        inputs_by_job={2: [shard, composite]},  # consumes the composite (+ a local shard)
        outputs_by_job={1: [composite], 2: []},  # get's only output is the composite
    )
    all_artifacts, _ = builder._collect_artifacts(steps_by_number)
    output_path_to_step = builder._map_output_paths(all_artifacts)
    output_artifact_to_step = builder._map_output_artifact_ids(all_artifacts)

    nodes = builder._build_nodes(
        [step1, step2], steps_by_number, set(), output_path_to_step, output_artifact_to_step, False
    )
    by_step = {n["step_number"]: n for n in nodes}

    assert by_step[1]["dependencies"] == []
    assert by_step[2]["dependencies"] == [1]  # @2 depends on @1 via the composite
    assert by_step[2]["metrics"]["consumed"] == 1


def test_no_edge_when_no_shared_artifact() -> None:
    a = {"artifact_id": "a", "kind": "primitive", "path": "/x/a", "hashes": []}
    b = {"artifact_id": "b", "kind": "primitive", "path": "/x/b", "hashes": []}
    step1 = {"id": 1, "step_number": 1, "timestamp": 1.0, "command": "one", "job_type": None}
    step2 = {"id": 2, "step_number": 2, "timestamp": 2.0, "command": "two", "job_type": None}
    steps_by_number = {1: [step1], 2: [step2]}

    builder = _builder(
        inputs_by_job={2: [b]},
        outputs_by_job={1: [a], 2: []},
    )
    all_artifacts, _ = builder._collect_artifacts(steps_by_number)
    nodes = builder._build_nodes(
        [step1, step2],
        steps_by_number,
        set(),
        builder._map_output_paths(all_artifacts),
        builder._map_output_artifact_ids(all_artifacts),
        False,
    )
    by_step = {n["step_number"]: n for n in nodes}
    assert by_step[2]["dependencies"] == []
