from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from roar.application.labels import (
    LabelService,
    _apply_published_remote_job_uids,
    _published_remote_session_hash,
    build_reconcile_payload_for_current_lineage,
    collect_label_sync_payloads,
    count_user_labels,
)
from roar.core.label_origins import build_current_key_origins


def test_count_user_labels_excludes_roar_namespace() -> None:
    """Only user-set keys count; reserved ``roar.*`` system labels are ignored."""
    payloads = [
        # job with one system namespace + two user labels
        {"metadata": {"roar": {"run_version": "0.3.4"}, "stage": "train", "owner": "chris"}},
        # artifact with a single user label
        {"metadata": {"i_love_roar": "yes"}},
        # entity with only system labels contributes nothing
        {"metadata": {"roar": {"reproducible": True}}},
        # defensive: non-dict metadata is skipped
        {"metadata": None},
    ]

    assert count_user_labels(payloads) == 3


def test_count_user_labels_empty() -> None:
    assert count_user_labels([]) == 0


def test_reject_reserved_keys_blocks_system_managed_dataset_labels() -> None:
    with pytest.raises(ValueError, match="Reserved label keys cannot be set manually"):
        LabelService._reject_reserved_keys(
            {
                "dataset": {
                    "id": "file:///data/train",
                    "modality": "tabular",
                }
            }
        )


def test_reject_reserved_keys_blocks_system_managed_roar_labels() -> None:
    with pytest.raises(ValueError, match="Reserved label keys cannot be set manually"):
        LabelService._reject_reserved_keys(
            {
                "roar": {
                    "git": {
                        "commit": "deadbeef",
                    }
                }
            }
        )


def test_build_current_key_origins_replays_user_and_system_versions() -> None:
    history = [
        {
            "metadata": {
                "dataset": {
                    "id": "file:///data/train",
                    "modality": "tabular",
                    "type": "dataset",
                }
            },
            "write_origin": "system",
        },
        {
            "metadata": {
                "dataset": {
                    "id": "file:///data/train",
                    "modality": "tabular",
                    "type": "dataset",
                },
                "owner": "ml",
                "stage": "gold",
            },
            "write_origin": "user",
        },
    ]

    assert build_current_key_origins(history) == {
        "dataset.id": "system",
        "dataset.modality": "system",
        "dataset.type": "system",
        "owner": "user",
        "stage": "user",
    }


def test_build_current_key_origins_falls_back_to_legacy_paths_when_write_origin_missing() -> None:
    history = [
        {
            "metadata": {
                "dataset": {
                    "id": "file:///data/train",
                    "type": "dataset",
                }
            }
        },
        {
            "metadata": {
                "dataset": {
                    "id": "file:///data/train",
                    "type": "dataset",
                },
                "name": "preprocess",
            }
        },
    ]

    assert build_current_key_origins(history) == {
        "dataset.id": "system",
        "dataset.type": "system",
        "name": "user",
    }


def test_collect_label_sync_payloads_includes_current_key_origins() -> None:
    histories: dict[tuple[str, object], list[dict[str, object]]] = {
        ("dag", 7): [
            {
                "metadata": {
                    "dataset": {
                        "type": "dataset",
                    }
                },
                "write_origin": "system",
            },
            {
                "metadata": {
                    "dataset": {
                        "type": "dataset",
                    },
                    "project": "forecasting",
                },
                "write_origin": None,
            },
        ],
        ("job", 11): [
            {
                "metadata": {"name": "preprocess", "phase": "prep"},
                "write_origin": "user",
            }
        ],
        ("artifact", "artifact-1"): [
            {
                "metadata": {"generated": {"phase": "profiled"}},
                "write_origin": "system",
            },
            {
                "metadata": {"generated": {"phase": "profiled"}, "stage": "gold"},
                "write_origin": "user",
            },
        ],
    }

    class StubLabels:
        def get_current(self, entity_type: str, **kwargs):
            key = (
                entity_type,
                kwargs.get("session_id")
                if entity_type == "dag"
                else kwargs.get("job_id")
                if entity_type == "job"
                else kwargs.get("artifact_id"),
            )
            rows = histories.get(key, [])
            return rows[-1] if rows else None

        def get_history(self, entity_type: str, **kwargs):
            key = (
                entity_type,
                kwargs.get("session_id")
                if entity_type == "dag"
                else kwargs.get("job_id")
                if entity_type == "job"
                else kwargs.get("artifact_id"),
            )
            return histories.get(key, [])

    payloads = collect_label_sync_payloads(
        SimpleNamespace(labels=StubLabels()),
        session_id=7,
        session_hash="s" * 64,
        jobs=[{"id": 11, "job_uid": "job-1"}],
        artifacts=[{"id": "artifact-1", "hash": "a" * 64}],
    )

    assert payloads == [
        {
            "entity_type": "dag",
            "session_hash": "s" * 64,
            "metadata": {"dataset": {"type": "dataset"}, "project": "forecasting"},
            "key_origins": {"dataset.type": "system", "project": "user"},
        },
        {
            "entity_type": "job",
            "session_hash": "s" * 64,
            "job_uid": "job-1",
            "metadata": {"name": "preprocess", "phase": "prep"},
            "key_origins": {"name": "user", "phase": "user"},
        },
        {
            "entity_type": "artifact",
            "session_hash": "s" * 64,
            "artifact_hash": "a" * 64,
            "metadata": {"generated": {"phase": "profiled"}, "stage": "gold"},
            "key_origins": {"generated.phase": "system", "stage": "user"},
        },
    ]


def test_published_glaas_mapping_reads_session_hash_and_job_uids() -> None:
    session = {
        "metadata": json.dumps(
            {
                "roar": {
                    "remote_publication": {
                        "glaas": {
                            "schema_version": 1,
                            "session_hash": "a" * 64,
                            "prepared_session_hash": "b" * 64,
                            "jobs": {"local-job-1": "remote-job-1"},
                        }
                    }
                }
            }
        )
    }

    assert _published_remote_session_hash(session) == "a" * 64
    assert _apply_published_remote_job_uids(
        [{"id": 11, "job_uid": "local-job-1"}, {"id": 12, "job_uid": "local-job-2"}],
        session,
    ) == [
        {"id": 11, "job_uid": "local-job-1", "remote_job_uid": "remote-job-1"},
        {"id": 12, "job_uid": "local-job-2"},
    ]


def test_build_reconcile_payload_requires_published_glaas_mapping(tmp_path: Path) -> None:
    local_hash = "c" * 64

    class StubSessions:
        def get_active(self):
            return {"id": 7, "hash": local_hash, "metadata": None}

        def get(self, session_id: int):
            assert session_id == 7
            return {"id": 7, "hash": local_hash, "metadata": None}

    with pytest.raises(ValueError) as exc_info:
        build_reconcile_payload_for_current_lineage(
            SimpleNamespace(sessions=StubSessions()),
            roar_dir=tmp_path / ".roar",
        )

    assert str(exc_info.value) == (
        "Selected lineage has not been registered to GLaaS yet. "
        f"Run `roar register {local_hash} --yes` or `roar put <artifact> <destination>` "
        "first, then retry `roar label sync`."
    )


def test_collect_label_sync_payloads_prefers_remote_job_uid_when_present() -> None:
    class StubLabels:
        def get_current(self, entity_type: str, **kwargs):
            if entity_type == "job" and kwargs.get("job_id") == 11:
                return {"metadata": {"phase": "prep"}, "write_origin": "user"}
            return None

        def get_history(self, entity_type: str, **kwargs):
            if entity_type == "job" and kwargs.get("job_id") == 11:
                return [{"metadata": {"phase": "prep"}, "write_origin": "user"}]
            return []

    payloads = collect_label_sync_payloads(
        SimpleNamespace(labels=StubLabels()),
        session_id=None,
        session_hash="s" * 64,
        jobs=[{"id": 11, "job_uid": "local-job-1", "remote_job_uid": "remote-job-1"}],
        artifacts=[],
    )

    assert payloads == [
        {
            "entity_type": "job",
            "session_hash": "s" * 64,
            "job_uid": "remote-job-1",
            "metadata": {"phase": "prep"},
            "key_origins": {"phase": "user"},
        }
    ]
