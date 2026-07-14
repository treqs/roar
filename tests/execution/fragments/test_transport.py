from __future__ import annotations

from typing import Any

import pytest

from roar.execution.fragments import transport


class _FakeStreamer:
    """Streamer double with a scripted close() outcome."""

    def __init__(
        self,
        *,
        close_result: bool = True,
        pending_after_close: int = 0,
        raise_on_append: bool = False,
    ) -> None:
        self._close_result = close_result
        self._pending_after_close = pending_after_close
        self._raise_on_append = raise_on_append
        self.appended: list[dict[str, Any]] = []

    def append_fragment(self, fragment: dict[str, Any]) -> None:
        if self._raise_on_append:
            raise RuntimeError("boom")
        self.appended.append(fragment)

    def close(self) -> bool:
        return self._close_result

    @property
    def pending_fragments(self) -> int:
        return self._pending_after_close


ENV = {
    "ROAR_SESSION_ID": "session-1",
    "ROAR_FRAGMENT_TOKEN": "ab" * 32,
    "GLAAS_URL": "http://localhost:3001",
    "ROAR_PROJECT_DIR": "/proj",
}


def _install_streamer(monkeypatch: pytest.MonkeyPatch, streamer: _FakeStreamer) -> None:
    monkeypatch.setattr(transport, "GlaasFragmentStreamer", lambda **kwargs: streamer)


def test_emit_returns_streamed_only_when_fully_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = _FakeStreamer(close_result=True)
    _install_streamer(monkeypatch, streamer)

    result = transport.emit_fragment_dicts([{"job_uid": "a"}], env=ENV)

    assert result == "streamed"
    assert streamer.appended == [{"job_uid": "a"}]


def test_emit_falls_back_to_local_merge_on_undelivered_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = _FakeStreamer(close_result=False, pending_after_close=2)
    _install_streamer(monkeypatch, streamer)
    merged: list[tuple[list[dict[str, Any]], str, str | None]] = []

    result = transport.emit_fragment_dicts(
        [{"job_uid": "a"}, {"job_uid": "b"}],
        env=ENV,
        local_merge=lambda fragments, project_dir, driver: merged.append(
            (fragments, project_dir, driver)
        ),
    )

    assert result == "merged"
    assert merged == [([{"job_uid": "a"}, {"job_uid": "b"}], "/proj", None)]


def test_emit_falls_back_to_local_fallback_on_streamer_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = _FakeStreamer(raise_on_append=True)
    _install_streamer(monkeypatch, streamer)
    fallback_calls: list[str] = []

    result = transport.emit_fragment_dicts(
        [{"job_uid": "a"}],
        env={key: value for key, value in ENV.items() if key != "ROAR_PROJECT_DIR"},
        local_fallback=lambda: fallback_calls.append("fallback"),
    )

    assert result == "fallback"
    assert fallback_calls == ["fallback"]


def test_emit_returns_skipped_when_no_fallback_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = _FakeStreamer(close_result=False, pending_after_close=1)
    _install_streamer(monkeypatch, streamer)

    result = transport.emit_fragment_dicts([{"job_uid": "a"}], env=ENV)

    assert result == "skipped"
