from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from roar.services.execution.fragment_sessions import (
    fragment_session_path,
    generate_fragment_session,
    load_fragment_session,
    save_fragment_session,
)


def test_generate_fragment_key_returns_uuid_and_32_byte_token() -> None:
    key = generate_fragment_session()

    parsed_session_id = uuid.UUID(key["session_id"])
    assert parsed_session_id.version == 4
    assert len(key["token"]) == 64
    int(key["token"], 16)


def test_token_hash_is_sha256_of_token() -> None:
    key = generate_fragment_session()

    expected_hash = hashlib.sha256(key["token"].encode("utf-8")).hexdigest()
    assert key["token_hash"] == expected_hash


def test_store_key_writes_json_file_to_roar_fragment_sessions_dir(tmp_path: Path) -> None:
    key = generate_fragment_session()

    path = save_fragment_session(tmp_path / ".roar", key)

    assert path == fragment_session_path(tmp_path / ".roar", key["session_id"])
    assert path.parent == tmp_path / ".roar" / "fragment-sessions"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_id"] == key["session_id"]
    assert payload["token"] == key["token"]


def test_load_key_round_trip_restores_session_id_and_token(tmp_path: Path) -> None:
    key = generate_fragment_session()
    roar_dir = tmp_path / ".roar"

    save_fragment_session(roar_dir, key)
    restored = load_fragment_session(roar_dir, key["session_id"])

    assert restored["session_id"] == key["session_id"]
    assert restored["token"] == key["token"]
    assert restored["token_hash"] == key["token_hash"]


def test_missing_key_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_fragment_session(tmp_path / ".roar", "00000000-0000-4000-8000-000000000000")
