from __future__ import annotations

import base64
import importlib
import json
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roar.ray.fragment_key import generate_fragment_key, load_key, save_key


def _module():
    return importlib.import_module("roar.ray.fragment_reconstituter")


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return self._body


def _encrypt_batch(token: str, fragments: list[dict], nonce_byte: int) -> str:
    key = bytes.fromhex(token)
    nonce = bytes([nonce_byte]) * 12
    plaintext = json.dumps(fragments, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def _fragments_payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps({"fragments": items}, separators=(",", ":")).encode("utf-8")


def test_reconstitute_fetches_decrypts_and_merges_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "ab" * 32
    session_id = "session-1"

    sequence_0 = [{"job_uid": "job-a"}]
    sequence_1 = [{"job_uid": "job-b"}]
    response_body = _fragments_payload(
        [
            {"sequence": 1, "encrypted_batch": _encrypt_batch(token, sequence_1, 1)},
            {"sequence": 0, "encrypted_batch": _encrypt_batch(token, sequence_0, 0)},
        ]
    )

    captured_requests: list[urllib.request.Request] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        captured_requests.append(request)
        assert timeout == 5
        return _FakeHttpResponse(response_body)

    merged_fragments: list[list[dict]] = []

    def _fake_collect_fragments(*args, **kwargs) -> None:
        if args:
            merged_fragments.append(list(args[0]))
            return
        merged_fragments.append(list(kwargs["fragments"]))

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", _fake_collect_fragments)

    result = module.FragmentReconstituter(
        session_id=session_id,
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.full_url.endswith(f"/api/v1/fragments/sessions/{session_id}/fragments")
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-roar-fragment-token"] == token

    assert merged_fragments == [[{"job_uid": "job-a"}, {"job_uid": "job-b"}]]
    assert result.fragments_processed == 2


def test_reconstitute_is_idempotent_for_same_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "cd" * 32
    session_id = "session-2"
    fragments = [{"job_uid": "job-a"}, {"job_uid": "job-b"}]
    response_body = _fragments_payload(
        [{"sequence": 0, "encrypted_batch": _encrypt_batch(token, fragments, 2)}]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    merged_fragments: list[list[dict]] = []

    def _fake_collect_fragments(*args, **kwargs) -> None:
        if args:
            merged_fragments.append(list(args[0]))
            return
        merged_fragments.append(list(kwargs["fragments"]))

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", _fake_collect_fragments)

    reconstituter = module.FragmentReconstituter(
        session_id=session_id,
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )
    first = reconstituter.reconstitute()
    second = reconstituter.reconstitute()

    assert merged_fragments == [fragments, fragments]
    assert first.fragments_processed == 2
    assert second.fragments_processed == 2


def test_reconstitute_noop_when_no_remote_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "ef" * 32

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(_fragments_payload([]))

    merge_calls: list[object] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: merge_calls.append(1))

    result = module.FragmentReconstituter(
        session_id="session-3",
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert merge_calls == []
    assert result.fragments_processed == 0
    assert result.jobs_merged == 0
    assert result.artifacts_merged == 0


def test_reconstitute_handles_bad_token_without_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    good_token = "01" * 32
    wrong_token = "02" * 32
    response_body = _fragments_payload(
        [
            {
                "sequence": 0,
                "encrypted_batch": _encrypt_batch(good_token, [{"job_uid": "job-a"}], 3),
            }
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    merge_calls: list[object] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: merge_calls.append(1))

    result = module.FragmentReconstituter(
        session_id="session-4",
        token=wrong_token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert merge_calls == []
    assert result.fragments_processed == 0


def test_fragment_key_file_is_retained_after_reconstitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    roar_dir = tmp_path / ".roar"
    key = generate_fragment_key()
    key_path = save_key(roar_dir, key)
    loaded_key = load_key(roar_dir, key["session_id"])
    response_body = _fragments_payload(
        [
            {
                "sequence": 0,
                "encrypted_batch": _encrypt_batch(str(loaded_key["token"]), [{"job_uid": "job-a"}], 4),
            }
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: None)

    module.FragmentReconstituter(
        session_id=key["session_id"],
        token=str(loaded_key["token"]),
        glaas_url="http://localhost:3001",
        roar_db_path=roar_dir / "roar.db",
    ).reconstitute()

    assert key_path.exists()
