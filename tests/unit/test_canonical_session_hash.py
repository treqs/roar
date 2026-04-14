from __future__ import annotations

import json

from roar.core.canonical_session import (
    compute_canonical_session_hash,
    serialize_canonical_session_payload,
)


def _payload(*, creator_identity: str = "treqs:user:user-123") -> dict:
    return {
        "canonical_version": 1,
        "creator_identity": creator_identity,
        "git": {
            "repo": "https://github.com/test/repo",
            "commit": "deadbeef",
            "branch": "main",
        },
        "jobs": [
            {
                "command": "python train.py",
                "job_type": "run",
                "step_number": 1,
                "parent_step_number": None,
                "inputs": [
                    {"hash": "input-b", "path": "data/b.csv"},
                    {"hash": "input-a", "path": "data/a.csv"},
                ],
                "outputs": [
                    {"hash": "output-b", "path": "models/b.bin"},
                    {"hash": "output-a", "path": "models/a.bin"},
                ],
                "metadata": {
                    "runner": "python",
                    "image": "python:3.12",
                },
            }
        ],
    }


def test_identical_canonical_payloads_produce_identical_hashes() -> None:
    left = _payload()
    right = _payload()

    assert compute_canonical_session_hash(left) == compute_canonical_session_hash(right)


def test_changing_creator_identity_changes_hash() -> None:
    left = _payload(creator_identity="treqs:user:user-123")
    right = _payload(creator_identity="treqs:user:user-456")

    assert compute_canonical_session_hash(left) != compute_canonical_session_hash(right)


def test_changing_lineage_inputs_changes_hash() -> None:
    left = _payload()
    right = _payload()
    right["jobs"][0]["inputs"][0]["hash"] = "different-input"

    assert compute_canonical_session_hash(left) != compute_canonical_session_hash(right)


def test_serialization_is_deterministic_for_unsorted_inputs_outputs_and_metadata() -> None:
    payload = _payload()

    serialized = serialize_canonical_session_payload(payload)
    decoded = json.loads(serialized)

    assert [item["hash"] for item in decoded["jobs"][0]["inputs"]] == ["input-a", "input-b"]
    assert [item["hash"] for item in decoded["jobs"][0]["outputs"]] == ["output-a", "output-b"]
    assert list(decoded["jobs"][0]["metadata"].keys()) == ["image", "runner"]


def test_canonical_hash_does_not_depend_on_local_path_or_session_id_fields() -> None:
    left = _payload()
    right = _payload()
    left["local_context"] = {"roar_dir": "/tmp/one/.roar", "session_id": 7}
    right["local_context"] = {"roar_dir": "/tmp/two/.roar", "session_id": 999}

    assert compute_canonical_session_hash(left) == compute_canonical_session_hash(right)
