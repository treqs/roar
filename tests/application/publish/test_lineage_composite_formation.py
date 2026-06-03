"""Tests for register-time composite formation from a run's recorded inputs/outputs.

`form_lineage_composites` is the register-side bridge: it runs the shared
``form_composites`` core over each job's leaf inputs and outputs, persists any detected
dataset as a local composite artifact (kind=composite), and links it to the job
(produced -> output edge, consumed -> input edge). The lineage is then re-collected, so
the existing bundle-registration and view-edge machinery surface it unchanged.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.application.publish.lineage_composite_formation import (
    form_lineage_composites,
    prune_composite_output_leaves,
)


def _h(name: str) -> str:
    return hashlib.blake2b(name.encode(), digest_size=32).hexdigest()


def _row(path: str, kind: str | None = None) -> dict:
    return {
        "path": path,
        "artifact_id": f"aid-{path}",
        "kind": kind,
        "hashes": [{"algorithm": "blake3", "digest": _h(path)}],
        "first_seen_path": path,
    }


def _db(*, inputs: dict[int, list[dict]], outputs: dict[int, list[dict]]):
    artifacts, composites, jobs = MagicMock(), MagicMock(), MagicMock()
    jobs.get_inputs.side_effect = lambda jid: inputs.get(jid, [])
    jobs.get_outputs.side_effect = lambda jid: outputs.get(jid, [])
    # register() returns a fresh id per distinct digest, created=True.
    minted: dict[str, str] = {}

    def _register(*, hashes, **_kw):
        digest = next(iter(hashes.values()))
        created = digest not in minted
        minted.setdefault(digest, f"comp-{len(minted)}")
        return minted[digest], created

    artifacts.register.side_effect = _register
    return SimpleNamespace(artifacts=artifacts, composites=composites, jobs=jobs)


def _repos(db):
    # form_lineage_composites resolves repos via optional_repo(db_ctx, name).
    import roar.application.publish.lineage_composite_formation as mod

    mod.optional_repo = lambda ctx, name: getattr(ctx, name)  # type: ignore[attr-defined]
    return db


def test_zarr_outputs_form_and_link_composite_to_producer():
    zarr = [
        _row("out/x.zarr/.zgroup"),
        _row("out/x.zarr/0"),
        _row("out/x.zarr/1"),
    ]
    db = _repos(_db(inputs={}, outputs={7: zarr}))
    added = form_lineage_composites(db_ctx=db, job_ids=[7], logger=MagicMock())

    assert added == 1
    db.composites.upsert_details.assert_called_once()
    # Linked as an OUTPUT of the producing job (not an input).
    db.jobs.add_output.assert_called_once()
    assert db.jobs.add_output.call_args.args[0] == 7
    db.jobs.add_input.assert_not_called()


def test_full_read_zarr_inputs_form_and_link_to_consumer():
    zarr = [_row("in/y.zarr/.zgroup"), _row("in/y.zarr/0"), _row("in/y.zarr/1")]
    db = _repos(_db(inputs={3: zarr}, outputs={}))
    added = form_lineage_composites(db_ctx=db, job_ids=[3], logger=MagicMock())

    assert added == 1
    db.jobs.add_input.assert_called_once()
    assert db.jobs.add_input.call_args.args[0] == 3
    db.jobs.add_output.assert_not_called()


def test_loose_outputs_form_nothing():
    loose = [_row("out/a.bin"), _row("out/b.bin"), _row("out/c.log")]
    db = _repos(_db(inputs={}, outputs={1: loose}))
    added = form_lineage_composites(db_ctx=db, job_ids=[1], logger=MagicMock())

    assert added == 0
    db.composites.upsert_details.assert_not_called()
    db.jobs.add_output.assert_not_called()


def test_loose_parquets_form_nothing_at_register():
    # The keystone policy at the register boundary: loose parquet a run touched is NOT a
    # dataset (no manifest). Only a declared put would composite it.
    shards = [_row("out/s/part-0.parquet"), _row("out/s/part-1.parquet")]
    db = _repos(_db(inputs={}, outputs={1: shards}))
    assert form_lineage_composites(db_ctx=db, job_ids=[1], logger=MagicMock()) == 0


def test_prune_composite_output_leaves_keeps_only_the_composite():
    # After a Zarr composite is formed as an output, the loose leaves under its root are
    # pruned from the bundle so the producer job shows ONLY the composite.
    rows = [
        {"path": "out/x.zarr/.zgroup", "artifact_hash": _h("a"), "kind": None},
        {"path": "out/x.zarr/0", "artifact_hash": _h("b"), "kind": None},
        {"path": "out/x.zarr", "artifact_hash": _h("comp"), "kind": "composite"},
        {"path": "out/model.pt", "artifact_hash": _h("m"), "kind": None},  # unrelated, kept
    ]
    jobs = MagicMock()
    jobs.get_outputs.side_effect = lambda jid: rows
    db = SimpleNamespace(jobs=jobs)
    import roar.application.publish.lineage_composite_formation as mod

    mod.optional_repo = lambda ctx, name: getattr(ctx, name)  # type: ignore[attr-defined]

    job = {
        "id": 9,
        "_output_hashes": [_h("a"), _h("b"), _h("comp"), _h("m")],
        "_outputs": [
            {"hash": _h("a"), "path": "out/x.zarr/.zgroup"},
            {"hash": _h("b"), "path": "out/x.zarr/0"},
            {"hash": _h("comp"), "path": "out/x.zarr"},
            {"hash": _h("m"), "path": "out/model.pt"},
        ],
    }
    pruned = prune_composite_output_leaves(db_ctx=db, lineage_jobs=[job], logger=MagicMock())

    assert pruned == 2
    # Composite + unrelated output remain; the two Zarr leaves are gone.
    assert job["_output_hashes"] == [_h("comp"), _h("m")]
    assert {o["hash"] for o in job["_outputs"]} == {_h("comp"), _h("m")}


def test_prune_noop_without_composite_outputs():
    rows = [
        {"path": "out/a.bin", "artifact_hash": _h("a"), "kind": None},
        {"path": "out/b.bin", "artifact_hash": _h("b"), "kind": None},
    ]
    jobs = MagicMock()
    jobs.get_outputs.side_effect = lambda jid: rows
    db = SimpleNamespace(jobs=jobs)
    import roar.application.publish.lineage_composite_formation as mod

    mod.optional_repo = lambda ctx, name: getattr(ctx, name)  # type: ignore[attr-defined]

    job = {"id": 1, "_output_hashes": [_h("a"), _h("b")], "_outputs": []}
    assert prune_composite_output_leaves(db_ctx=db, lineage_jobs=[job], logger=MagicMock()) == 0
    assert job["_output_hashes"] == [_h("a"), _h("b")]


def test_existing_composite_rows_are_skipped_idempotent():
    # A second register pass: the composite is already a kind=composite input/output and
    # the leaves remain. It must not re-link (added == 0) so no needless re-collect.
    zarr_leaves = [_row("out/z.zarr/.zgroup"), _row("out/z.zarr/0"), _row("out/z.zarr/1")]
    composite_row = _row("out/z.zarr", kind="composite")
    composite_row["artifact_id"] = "comp-0"
    outputs = {5: [*zarr_leaves, composite_row]}
    db = _repos(_db(inputs={}, outputs=outputs))
    # add_output is a no-op for an already-linked (job, artifact, path); model that by
    # reporting the composite artifact id as already linked.
    added = form_lineage_composites(db_ctx=db, job_ids=[5], logger=MagicMock())
    assert added == 0
    db.jobs.add_output.assert_not_called()
