"""P0-22: finalize expectations must content-dedup, matching the server, so a
workload that writes identical bytes to two names (timm os.link last/best/
checkpoint) doesn't fail finalize with 'Staged lineage counts did not match'.
"""

from __future__ import annotations

from roar.application.publish.session import build_staged_lineage_counts


def _job(inputs=(), outputs=()):
    return {"_inputs": list(inputs), "_outputs": list(outputs)}


def test_hardlink_duplicate_outputs_collapse_to_one():
    # last.pth.tar / checkpoint-N.pth.tar / model_best.pth.tar: one inode, 3 names.
    dup = [
        {"hash": "abc", "path": "last.pth.tar", "byte_ranges": None},
        {"hash": "abc", "path": "checkpoint-9.pth.tar", "byte_ranges": None},
        {"hash": "abc", "path": "model_best.pth.tar", "byte_ranges": None},
        {"hash": "def", "path": "config.json", "byte_ranges": None},
    ]
    counts = build_staged_lineage_counts([_job(outputs=dup)])
    assert counts["outputs"] == 2  # 3 byte-identical -> 1, plus config -> 2
    assert counts["jobs"] == 1


def test_no_duplicates_is_a_noop_equal_to_path_count():
    outs = [
        {"hash": "a", "path": "p1", "byte_ranges": None},
        {"hash": "b", "path": "p2", "byte_ranges": None},
        {"hash": "c", "path": "p3", "byte_ranges": None},
    ]
    assert build_staged_lineage_counts([_job(outputs=outs)])["outputs"] == 3


def test_distinct_partial_reads_are_not_collapsed():
    # same file (hash), different byte ranges -> genuinely distinct input edges.
    ins = [
        {"hash": "z", "path": "data.bin", "byte_ranges": [[0, 100]]},
        {"hash": "z", "path": "data.bin", "byte_ranges": [[100, 200]]},
    ]
    assert build_staged_lineage_counts([_job(inputs=ins)])["inputs"] == 2


def test_edges_without_a_hash_are_ignored():
    outs = [{"hash": "", "path": "x", "byte_ranges": None}, {"path": "y"}]
    assert build_staged_lineage_counts([_job(outputs=outs)])["outputs"] == 0
