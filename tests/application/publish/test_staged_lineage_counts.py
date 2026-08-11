"""P0-22 (Option B): roar's finalize count AND canonical session hash must dedup
job edges by CONTENT hash, matching glaas's (job_id, artifact_hash) storage key —
so a workload writing identical bytes to two names (timm os.link last/best/
checkpoint) doesn't 400 finalize or diverge from glaas's published hash.
"""

from __future__ import annotations

from roar.application.publish.session import (
    _dedup_edges_by_hash,
    build_staged_lineage_counts,
)


def _job(inputs=(), outputs=()):
    return {"_inputs": list(inputs), "_outputs": list(outputs)}


def test_hardlink_duplicate_outputs_collapse_to_one_content():
    dup = [
        {"hash": "abc", "path": "last.pth.tar", "byte_ranges": None},
        {"hash": "abc", "path": "checkpoint-9.pth.tar", "byte_ranges": None},
        {"hash": "abc", "path": "model_best.pth.tar", "byte_ranges": None},
        {"hash": "def", "path": "config.json", "byte_ranges": None},
    ]
    assert build_staged_lineage_counts([_job(outputs=dup)])["outputs"] == 2


def test_no_duplicates_is_a_noop_equal_to_path_count():
    outs = [{"hash": h, "path": h, "byte_ranges": None} for h in ("a", "b", "c")]
    assert build_staged_lineage_counts([_job(outputs=outs)])["outputs"] == 3


def test_dedup_keeps_smallest_path_deterministically():
    # matches glaas skipDuplicates when staged in sorted order; stable representative
    edges = [
        {"hash": "x", "path": "zzz"},
        {"hash": "x", "path": "aaa"},
        {"hash": "x", "path": "mmm"},
    ]
    kept = _dedup_edges_by_hash(edges)
    assert len(kept) == 1 and kept[0]["path"] == "aaa"


def test_dedup_is_by_hash_not_byte_ranges():
    # glaas's key ignores byte_ranges, so same hash + different ranges still collapses
    edges = [
        {"hash": "z", "path": "d", "byte_ranges": [[0, 100]]},
        {"hash": "z", "path": "d", "byte_ranges": [[100, 200]]},
    ]
    assert len(_dedup_edges_by_hash(edges)) == 1


def test_edges_without_a_hash_are_dropped():
    assert (
        build_staged_lineage_counts([_job(outputs=[{"path": "x"}, {"hash": ""}])])["outputs"] == 0
    )
