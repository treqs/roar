from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roar.integrations.glaas import GlaasFragmentStreamer


class _FakeHttpResponse:
    def __init__(self, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def token() -> str:
    return "ab" * 32


def _decrypt_fragment_count(request: urllib.request.Request, token: str) -> int:
    payload = json.loads(request.data.decode("utf-8"))
    encrypted_batch = base64.b64decode(payload["encrypted_batch"])
    nonce = encrypted_batch[:12]
    ciphertext = encrypted_batch[12:]
    plaintext = AESGCM(bytes.fromhex(token)).decrypt(nonce, ciphertext, None)
    decoded = json.loads(plaintext.decode("utf-8"))
    assert isinstance(decoded, list)
    return len(decoded)


def test_enqueue_buffers_fragment_until_threshold(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    streamer = GlaasFragmentStreamer(
        session_id="session-123",
        token=token,
        glaas_url="http://localhost:3001",
        flush_threshold=2,
    )

    flush_calls: list[str] = []
    monkeypatch.setattr(streamer, "flush", lambda: flush_calls.append("flush") or True)

    streamer.append_fragment({"job_uid": "job-1"})
    assert streamer._buffer == [{"job_uid": "job-1"}]
    assert flush_calls == []

    streamer.append_fragment({"job_uid": "job-2"})
    assert flush_calls == ["flush"]


def test_flush_posts_encrypted_batch_to_glaas(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    captured: dict[str, object] = {}

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr("os.urandom", lambda n: b"\\x01" * n)

    streamer = GlaasFragmentStreamer(
        session_id="session-abc",
        token=token,
        glaas_url="http://localhost:3001",
        flush_threshold=10,
    )

    fragment = {"job_uid": "job-1", "ray_task_id": "task-1"}
    streamer.append_fragment(fragment)

    assert streamer.flush() is True

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == (
        "http://localhost:3001/api/v1/fragments/sessions/session-abc/fragments"
    )
    assert request.get_method() == "POST"

    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-roar-fragment-token"] == token

    body = json.loads(request.data.decode("utf-8"))
    assert body["sequence"] == 0
    assert isinstance(body["encrypted_batch"], str)
    assert body["encrypted_batch"]

    assert streamer._buffer == []
    assert streamer._next_sequence == 1


def test_flush_sequence_numbers_are_monotonically_increasing(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    posted_sequences: list[int] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        payload = json.loads(request.data.decode("utf-8"))
        posted_sequences.append(int(payload["sequence"]))
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-seq",
        token=token,
        glaas_url="http://localhost:3001",
    )

    streamer.append_fragment({"job_uid": "job-1"})
    assert streamer.flush() is True

    streamer.append_fragment({"job_uid": "job-2"})
    assert streamer.flush() is True

    assert posted_sequences == [0, 1]


def test_ciphertext_differs_from_plaintext_fragment_json(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    posted_batch: dict[str, str] = {}

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        payload = json.loads(request.data.decode("utf-8"))
        posted_batch["encrypted_batch"] = payload["encrypted_batch"]
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr("os.urandom", lambda n: b"\\x0f" * n)

    fragment = {
        "job_uid": "job-opaque",
        "ray_task_id": "task-opaque",
        "marker": "KNOWN_PLAINTEXT_MARKER",
    }
    streamer = GlaasFragmentStreamer(
        session_id="session-cipher",
        token=token,
        glaas_url="http://localhost:3001",
    )

    streamer.append_fragment(fragment)
    assert streamer.flush() is True

    plaintext = json.dumps([fragment], separators=(",", ":")).encode("utf-8")
    ciphertext_bytes = base64.b64decode(posted_batch["encrypted_batch"])

    assert ciphertext_bytes != plaintext
    assert plaintext not in ciphertext_bytes


def test_flush_keeps_buffer_when_post_fails(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-fail",
        token=token,
        glaas_url="http://localhost:3001",
    )
    fragment = {"job_uid": "job-lost"}
    streamer.append_fragment(fragment)

    assert streamer.flush() is False
    assert streamer._buffer == [fragment]
    assert streamer._next_sequence == 0


def test_flush_splits_oversized_batches_after_413(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    posted_sizes: list[int] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        fragment_count = _decrypt_fragment_count(request, token)
        if fragment_count > 1:
            raise urllib.error.HTTPError(
                request.full_url,
                413,
                "Payload Too Large",
                hdrs=None,
                fp=None,
            )
        posted_sizes.append(fragment_count)
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-split",
        token=token,
        glaas_url="http://localhost:3001",
    )
    streamer.append_fragment({"job_uid": "job-1"})
    streamer.append_fragment({"job_uid": "job-2"})
    streamer.append_fragment({"job_uid": "job-3"})

    assert streamer.flush() is True
    assert posted_sizes == [1, 1, 1]
    assert streamer._buffer == []
    assert streamer._next_sequence == 3


def test_flush_splits_oversized_single_fragment_by_refs(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    posted_ref_counts: list[tuple[int, int]] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        payload = json.loads(request.data.decode("utf-8"))
        encrypted_batch = base64.b64decode(payload["encrypted_batch"])
        nonce = encrypted_batch[:12]
        ciphertext = encrypted_batch[12:]
        plaintext = AESGCM(bytes.fromhex(token)).decrypt(nonce, ciphertext, None)
        decoded = json.loads(plaintext.decode("utf-8"))
        assert isinstance(decoded, list)
        total_reads = sum(len(fragment.get("reads", [])) for fragment in decoded)
        total_writes = sum(len(fragment.get("writes", [])) for fragment in decoded)
        if total_reads + total_writes > 1:
            raise urllib.error.HTTPError(
                request.full_url,
                413,
                "Payload Too Large",
                hdrs=None,
                fp=None,
            )
        posted_ref_counts.extend(
            (len(fragment.get("reads", [])), len(fragment.get("writes", [])))
            for fragment in decoded
        )
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-split-fragment",
        token=token,
        glaas_url="http://localhost:3001",
    )
    streamer.append_fragment(
        {
            "job_uid": "job-big",
            "reads": [{"path": "/tmp/input-a"}, {"path": "/tmp/input-b"}],
            "writes": [{"path": "/tmp/output-a"}, {"path": "/tmp/output-b"}],
        }
    )

    assert streamer.flush() is True
    assert posted_ref_counts == [(1, 0), (1, 0), (0, 1), (0, 1)]
    assert streamer._buffer == []
    assert streamer._next_sequence == 4


def test_post_403_renews_session_and_retries(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    state = {"renewed": False}
    renew_requests: list[urllib.request.Request] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        if request.full_url.endswith("/renew"):
            renew_requests.append(request)
            state["renewed"] = True
            return _FakeHttpResponse(status=200)
        if not state["renewed"]:
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)
        return _FakeHttpResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-expired",
        token=token,
        glaas_url="http://localhost:3001",
        renew_ttl_seconds=1234,
    )
    streamer.append_fragment({"job_uid": "job-late"})

    assert streamer.flush() is True
    assert streamer._buffer == []
    assert streamer.delivered_batches == 1
    assert streamer.failed_batches == 0

    assert len(renew_requests) == 1
    renew = renew_requests[0]
    assert renew.full_url == (
        "http://localhost:3001/api/v1/fragments/sessions/session-expired/renew"
    )
    headers = {name.lower(): value for name, value in renew.header_items()}
    assert headers["x-roar-fragment-token"] == token
    assert json.loads(renew.data.decode("utf-8")) == {"ttl_seconds": 1234}


def test_post_403_gives_up_when_renew_fails(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        if request.full_url.endswith("/renew"):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-gone",
        token=token,
        glaas_url="http://localhost:3001",
    )
    fragment = {"job_uid": "job-lost"}
    streamer.append_fragment(fragment)

    assert streamer.flush() is False
    assert streamer._buffer == [fragment]
    assert streamer.pending_fragments == 1
    assert streamer.failed_batches == 1


def test_post_403_after_successful_renew_gives_up(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    post_attempts = {"count": 0}

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del timeout
        if request.full_url.endswith("/renew"):
            return _FakeHttpResponse(status=200)
        post_attempts["count"] += 1
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    streamer = GlaasFragmentStreamer(
        session_id="session-still-forbidden",
        token=token,
        glaas_url="http://localhost:3001",
    )
    streamer.append_fragment({"job_uid": "job-1"})

    assert streamer.flush() is False
    assert post_attempts["count"] == 2


def test_close_flushes_remaining_fragments(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    streamer = GlaasFragmentStreamer(
        session_id="session-close",
        token=token,
        glaas_url="http://localhost:3001",
        flush_threshold=100,
    )
    streamer.append_fragment({"job_uid": "job-close"})

    flush_calls: list[str] = []
    monkeypatch.setattr(streamer, "flush", lambda: flush_calls.append("flush") or True)

    streamer.close()

    assert flush_calls == ["flush"]
