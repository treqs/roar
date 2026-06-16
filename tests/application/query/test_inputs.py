"""Application tests for inputs-query orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import roar.application.query.inputs as inputs_module
from roar.application.query.inputs import InputsQueryError
from roar.application.query.requests import InputsQueryRequest
from roar.application.query.results import InputsSummary


def _request(
    tmp_path: Path,
    ref: str,
    *,
    selector: str = "auto",
    direct: bool = False,
    show_all: bool = False,
    output_json: bool = False,
    unsourced: bool = False,
    sourced: bool = False,
) -> InputsQueryRequest:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    return InputsQueryRequest(
        roar_dir=roar_dir,
        cwd=tmp_path,
        ref=ref,
        selector=selector,
        direct=direct,
        show_all=show_all,
        output_json=output_json,
        unsourced=unsourced,
        sourced=sourced,
    )


def _artifact(artifact_id: str, path: str, digest: str, size: int = 1024) -> dict:
    return {
        "id": artifact_id,
        "size": size,
        "first_seen_at": 1700000000.0,
        "first_seen_path": path,
        "metadata": None,
        "hashes": [{"algorithm": "blake3", "digest": digest}],
    }


# -- root traversal (default mode) --


def test_build_inputs_walks_to_root_artifacts(tmp_path: Path) -> None:
    """model.pkl -> train.py -> features.npz -> extract.py -> data.parquet (root)."""
    model = _artifact("art-model", "/w/model.pkl", "a" * 64)
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = model

        # model produced by job 5, features produced by job 3, parquet is a root
        db_ctx.artifacts.get_jobs.side_effect = [
            # is_root check for model
            {"produced_by": [{"id": 5, "command": "python train.py"}], "consumed_by": []},
            # _trace(art-model): has producer
            {"produced_by": [{"id": 5, "command": "python train.py"}], "consumed_by": []},
            # _trace(art-feat): has producer
            {"produced_by": [{"id": 3, "command": "python extract.py"}], "consumed_by": []},
            # _trace(art-parq): no producer
            {"produced_by": [], "consumed_by": []},
        ]
        db_ctx.jobs.get_inputs.side_effect = [
            [{"artifact_id": "art-feat"}],  # is_root check: job 5 has inputs
            [{"artifact_id": "art-feat"}],  # _trace: job 5 inputs
            [{"artifact_id": "art-parq"}],  # _trace: job 3 inputs
        ]
        db_ctx.artifacts.get.return_value = parquet

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "model.pkl"))

    assert isinstance(summary, InputsSummary)
    assert not summary.is_root
    assert len(summary.artifacts) == 1
    assert summary.artifacts[0].artifact_id == "art-parq"


def test_build_inputs_returns_root_message_for_root_artifact(tmp_path: Path) -> None:
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = parquet
        # No producer at all
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}
        db_ctx.jobs.get_inputs.return_value = []

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "data.parquet"))

    assert summary.is_root
    assert "root artifact" in summary.message


def test_build_inputs_treats_roar_get_as_root(tmp_path: Path) -> None:
    """An artifact produced by roar get (producer has no inputs) is a root."""
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = parquet
        db_ctx.artifacts.get_jobs.return_value = {
            "produced_by": [{"id": 1, "command": "roar get https://..."}],
            "consumed_by": [],
        }
        db_ctx.jobs.get_inputs.return_value = []  # roar get has no inputs

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "data.parquet"))

    assert summary.is_root
    assert "root artifact" in summary.message


# -- direct mode --


def test_build_inputs_direct_returns_immediate_inputs_only(tmp_path: Path) -> None:
    model = _artifact("art-model", "/w/model.pkl", "a" * 64)
    features = _artifact("art-feat", "/w/features.npz", "b" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = model

        db_ctx.artifacts.get_jobs.side_effect = [
            # is_root check
            {"produced_by": [{"id": 5}], "consumed_by": []},
            # _get_direct_inputs lookup
            {"produced_by": [{"id": 5}], "consumed_by": []},
            # _is_unsourced(art-feat): has a producer -> sourced
            {"produced_by": [{"id": 3}], "consumed_by": []},
        ]
        db_ctx.jobs.get_inputs.side_effect = [
            # is_root check: job 5 has inputs, not a root
            [{"artifact_id": "art-feat"}],
            # direct: job 5 inputs
            [{"artifact_id": "art-feat"}],
        ]
        db_ctx.artifacts.get.return_value = features

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "model.pkl", direct=True))

    assert len(summary.artifacts) == 1
    assert summary.artifacts[0].artifact_id == "art-feat"


# -- all mode --


def test_build_inputs_all_includes_intermediates(tmp_path: Path) -> None:
    model = _artifact("art-model", "/w/model.pkl", "a" * 64)
    features = _artifact("art-feat", "/w/features.npz", "b" * 64)
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = model

        db_ctx.artifacts.get_jobs.side_effect = [
            # is_root check
            {"produced_by": [{"id": 5}], "consumed_by": []},
            # _trace(art-model): has producer
            {"produced_by": [{"id": 5}], "consumed_by": []},
            # _trace(art-feat): has producer
            {"produced_by": [{"id": 3}], "consumed_by": []},
            # _trace(art-parq): no producer
            {"produced_by": [], "consumed_by": []},
        ]
        db_ctx.jobs.get_inputs.side_effect = [
            # is_root check
            [{"artifact_id": "art-feat"}],
            # job 5 inputs
            [{"artifact_id": "art-feat"}],
            # job 3 inputs
            [{"artifact_id": "art-parq"}],
        ]
        db_ctx.artifacts.get.side_effect = [features, parquet]

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "model.pkl", show_all=True))

    artifact_ids = {a.artifact_id for a in summary.artifacts}
    assert "art-feat" in artifact_ids
    assert "art-parq" in artifact_ids
    assert "art-model" not in artifact_ids  # target excluded


# -- job ref --


def test_build_inputs_resolves_job_step_ref(tmp_path: Path) -> None:
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = {"id": 1}
        db_ctx.sessions.get_step_by_number.return_value = {
            "id": 5,
            "job_uid": "b108cdb6",
            "command": "python train.py",
        }
        # Job 5 has one input: parquet (a root)
        db_ctx.jobs.get_inputs.side_effect = [
            [{"artifact_id": "art-parq"}],  # resolve job target
            [],  # is_root check: roar get has no inputs
        ]
        db_ctx.artifacts.get_jobs.return_value = {
            "produced_by": [{"id": 1, "command": "roar get"}],
            "consumed_by": [],
        }
        db_ctx.artifacts.get.return_value = parquet

        summary = inputs_module.build_inputs_summary(_request(tmp_path, "@5"))

    assert summary.is_root
    assert "root artifact" in summary.message


# -- error cases --


def test_build_inputs_raises_for_untracked_artifact(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = None

        with pytest.raises(InputsQueryError, match="is not a tracked artifact"):
            inputs_module.build_inputs_summary(_request(tmp_path, "missing.bin"))


def test_build_inputs_raises_for_missing_job_ref(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = {"id": 1}
        db_ctx.sessions.get_step_by_number.return_value = None

        with pytest.raises(InputsQueryError, match="Job not found"):
            inputs_module.build_inputs_summary(_request(tmp_path, "@99"))


def test_build_inputs_raises_without_active_session_for_job_ref(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = None

        with pytest.raises(InputsQueryError, match="No active session"):
            inputs_module.build_inputs_summary(_request(tmp_path, "@1"))


# -- json output --


def test_render_inputs_json_output(tmp_path: Path) -> None:
    parquet = _artifact("art-parq", "/w/data.parquet", "c" * 64)

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = parquet
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

        output = inputs_module.render_inputs(_request(tmp_path, "data.parquet", output_json=True))

    assert '"is_root": true' in output
    assert '"target": "data.parquet"' in output


# -- unsourced vs sourced detection (reproducibility audit) --


def _audit_db(db_ctx) -> None:
    """Wire a keyed mock DB for a 1-job lineage:

    out.pkl  <- job 1  (inputs: gen.py [unsourced root], data.csv [sourced via job 0])
    gen.py   : no producer            -> unsourced
    data.csv : produced by job 0 (roar get, no inputs) -> sourced root
    """
    producers = {
        "art-out": [{"id": 1, "command": "python proc.py"}],
        "art-gen": [],  # no producer -> unsourced
        "art-data": [{"id": 0, "command": "roar get s3://b/data.csv"}],
    }
    job_inputs = {
        1: [{"artifact_id": "art-gen"}, {"artifact_id": "art-data"}],
        0: [],  # roar get: producer with no inputs -> sourced
    }
    arts = {
        "art-gen": _artifact("art-gen", "/w/gen.py", "b" * 64),
        "art-data": _artifact("art-data", "/w/data.csv", "c" * 64),
    }
    db_ctx.artifacts.get_by_path.return_value = _artifact("art-out", "/w/out.pkl", "a" * 64)
    db_ctx.artifacts.get_jobs.side_effect = lambda aid: {
        "produced_by": producers.get(aid, []),
        "consumed_by": [],
    }
    db_ctx.jobs.get_inputs.side_effect = lambda jid: job_inputs.get(jid, [])
    db_ctx.artifacts.get.side_effect = lambda aid: arts.get(aid)


def test_no_producer_root_is_unsourced_ingested_root_is_sourced(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        _audit_db(db_ctx)
        summary = inputs_module.build_inputs_summary(_request(tmp_path, "out.pkl"))

    by_path = {a.path: a.unsourced for a in summary.artifacts}
    assert by_path == {"/w/gen.py": True, "/w/data.csv": False}


def test_unsourced_filter_keeps_only_unsourced(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        _audit_db(db_ctx)
        summary = inputs_module.build_inputs_summary(_request(tmp_path, "out.pkl", unsourced=True))
    assert [a.path for a in summary.artifacts] == ["/w/gen.py"]


def test_sourced_filter_keeps_only_sourced(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        _audit_db(db_ctx)
        summary = inputs_module.build_inputs_summary(_request(tmp_path, "out.pkl", sourced=True))
    assert [a.path for a in summary.artifacts] == ["/w/data.csv"]


def test_render_marks_unsourced_and_shows_legend(tmp_path: Path) -> None:
    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        _audit_db(db_ctx)
        out = inputs_module.render_inputs(_request(tmp_path, "out.pkl"))
    assert "⚠ /w/gen.py" in out
    assert "⚠ unsourced:" in out  # legend present
    # sourced one is shown but not marked
    assert "/w/data.csv" in out and "⚠ /w/data.csv" not in out


def test_json_output_includes_unsourced_field(tmp_path: Path) -> None:
    import json

    with patch.object(inputs_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        _audit_db(db_ctx)
        out = inputs_module.render_inputs(_request(tmp_path, "out.pkl", output_json=True))
    data = json.loads(out)
    flags = {i["path"]: i["unsourced"] for i in data["inputs"]}
    assert flags == {"/w/gen.py": True, "/w/data.csv": False}
