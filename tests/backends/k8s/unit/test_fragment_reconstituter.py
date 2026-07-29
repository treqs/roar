"""Unit tests for k8s fragment deduplication at reconstitution."""

from __future__ import annotations

from roar.backends.k8s.fragment_reconstituter import K8sFragmentReconstituter


def _fragment(identity: str, reads: list[dict], writes: list[dict], **extra) -> dict:
    return {
        "task_identity": identity,
        "reads": reads,
        "writes": writes,
        **extra,
    }


def test_dedup_merges_oversized_split_parts() -> None:
    """413-split parts share one identity; no reference may be discarded."""
    part_one = _fragment(
        "pod-1:trainer:0:0",
        reads=[{"path": "/data/a.csv", "hash": "aa"}],
        writes=[{"path": "/out/model.bin", "hash": "mm"}],
    )
    part_two = _fragment(
        "pod-1:trainer:0:0",
        reads=[{"path": "/data/b.csv", "hash": "bb"}],
        writes=[{"path": "/out/metrics.json", "hash": "jj"}],
    )

    result = K8sFragmentReconstituter._deduplicate_by_task_identity([part_one, part_two])

    assert len(result) == 1
    merged = result[0]
    assert {ref["path"] for ref in merged["reads"]} == {"/data/a.csv", "/data/b.csv"}
    assert {ref["path"] for ref in merged["writes"]} == {
        "/out/model.bin",
        "/out/metrics.json",
    }


def test_dedup_collapses_duplicate_deliveries_without_duplicating_refs() -> None:
    """Stream + bundle double-delivery of the same fragment stays one fragment."""
    fragment = _fragment(
        "pod-1:trainer:0:0",
        reads=[{"path": "/data/a.csv", "hash": "aa"}],
        writes=[{"path": "/out/model.bin", "hash": "mm"}],
    )

    result = K8sFragmentReconstituter._deduplicate_by_task_identity(
        [dict(fragment), dict(fragment)]
    )

    assert len(result) == 1
    assert len(result[0]["reads"]) == 1
    assert len(result[0]["writes"]) == 1


def test_dedup_last_ref_wins_per_path_and_scalars_from_last_fragment() -> None:
    earlier = _fragment(
        "pod-1:trainer:0:0",
        reads=[{"path": "/data/a.csv", "hash": "stale"}],
        writes=[],
        exit_code=1,
    )
    later = _fragment(
        "pod-1:trainer:0:0",
        reads=[{"path": "/data/a.csv", "hash": "fresh"}],
        writes=[],
        exit_code=0,
    )

    result = K8sFragmentReconstituter._deduplicate_by_task_identity([earlier, later])

    assert len(result) == 1
    assert result[0]["reads"] == [{"path": "/data/a.csv", "hash": "fresh"}]
    assert result[0]["exit_code"] == 0


def test_dedup_keeps_distinct_identities_separate() -> None:
    rank0 = _fragment("pod-1:trainer:0:0", reads=[], writes=[{"path": "/out/w0"}])
    rank1 = _fragment("pod-2:trainer:1:0", reads=[], writes=[{"path": "/out/w1"}])

    result = K8sFragmentReconstituter._deduplicate_by_task_identity([rank0, rank1])

    assert len(result) == 2


def test_dedup_fragments_without_identity_are_not_conflated() -> None:
    anon_one = _fragment("", reads=[{"path": "/a"}], writes=[])
    anon_two = _fragment("", reads=[{"path": "/b"}], writes=[])

    result = K8sFragmentReconstituter._deduplicate_by_task_identity([anon_one, anon_two])

    assert len(result) == 2
