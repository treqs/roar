"""Unit tests for crosswalk-based job -> dataset-anchor attribution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.application.publish.anchor_attribution import attribute_jobs_to_anchors


def _db(*, inputs, artifacts, anchors_by_sha):
    """Build a fake db_ctx.

    inputs: list of input dicts for the (single) job.
    artifacts: {artifact_id: {"hashes": [{"algorithm","digest"}], "first_seen_path": ...}}
    anchors_by_sha: {sha256_digest: [anchor_artifact_id, ...]}
    """
    jobs = MagicMock()
    jobs.get_inputs.return_value = inputs

    artifacts_repo = MagicMock()
    artifacts_repo.get.side_effect = lambda aid: artifacts.get(aid)

    composites = MagicMock()
    composites.find_by_component_digest.side_effect = lambda digest, algo="sha256": (
        anchors_by_sha.get(digest, [])
    )
    return SimpleNamespace(jobs=jobs, artifacts=artifacts_repo, composites=composites)


def test_attributes_run_input_to_its_anchor():
    db = _db(
        inputs=[{"artifact_id": "shard-1", "path": "/local/train-0.parquet"}],
        artifacts={
            "shard-1": {
                "hashes": [
                    {"algorithm": "blake3", "digest": "b" * 64},
                    {"algorithm": "sha256", "digest": "s" * 64},
                ]
            },
            "anchor-1": {"first_seen_path": "hf://datasets/x/y@abc"},
        },
        anchors_by_sha={"s" * 64: ["anchor-1"]},
    )
    added = attribute_jobs_to_anchors(db_ctx=db, job_ids=[1], logger=MagicMock())
    assert added == 1
    db.jobs.add_input.assert_called_once_with(1, "anchor-1", "hf://datasets/x/y@abc")


def test_attributes_via_crosswalk_sha256_in_metadata():
    # F1: the shard publishes blake3 only; its sha256 crosswalk lives in metadata.
    # Attribution must resolve the anchor from metadata, not a published sha256 hash.
    import json

    db = _db(
        inputs=[{"artifact_id": "shard-1", "path": "/local/train-0.parquet"}],
        artifacts={
            "shard-1": {
                "hashes": [{"algorithm": "blake3", "digest": "b" * 64}],
                "metadata": json.dumps({"origin": {"algorithm": "sha256", "digest": "s" * 64}}),
            },
            "anchor-1": {"first_seen_path": "hf://datasets/x/y@abc"},
        },
        anchors_by_sha={"s" * 64: ["anchor-1"]},
    )
    assert attribute_jobs_to_anchors(db_ctx=db, job_ids=[1], logger=MagicMock()) == 1
    db.jobs.add_input.assert_called_once_with(1, "anchor-1", "hf://datasets/x/y@abc")


def test_no_sha256_crosswalk_means_no_attribution():
    db = _db(
        inputs=[{"artifact_id": "blob-1", "path": "/local/f.bin"}],
        artifacts={"blob-1": {"hashes": [{"algorithm": "blake3", "digest": "b" * 64}]}},
        anchors_by_sha={},
    )
    assert attribute_jobs_to_anchors(db_ctx=db, job_ids=[1], logger=MagicMock()) == 0
    db.jobs.add_input.assert_not_called()


def test_skips_when_anchor_already_an_input():
    # The anchor is already linked as an input — don't double-link.
    db = _db(
        inputs=[
            {"artifact_id": "shard-1", "path": "/local/train-0.parquet"},
            {"artifact_id": "anchor-1", "path": "hf://datasets/x/y@abc"},
        ],
        artifacts={
            "shard-1": {"hashes": [{"algorithm": "sha256", "digest": "s" * 64}]},
            "anchor-1": {"first_seen_path": "hf://datasets/x/y@abc"},
        },
        anchors_by_sha={"s" * 64: ["anchor-1"]},
    )
    assert attribute_jobs_to_anchors(db_ctx=db, job_ids=[1], logger=MagicMock()) == 0
    db.jobs.add_input.assert_not_called()


def test_does_not_attribute_anchor_to_itself():
    # A composite's own component digest must not link the anchor to itself.
    db = _db(
        inputs=[{"artifact_id": "anchor-1", "path": "hf://datasets/x/y@abc"}],
        artifacts={"anchor-1": {"hashes": [{"algorithm": "sha256", "digest": "s" * 64}]}},
        anchors_by_sha={"s" * 64: ["anchor-1"]},
    )
    assert attribute_jobs_to_anchors(db_ctx=db, job_ids=[1], logger=MagicMock()) == 0
    db.jobs.add_input.assert_not_called()
