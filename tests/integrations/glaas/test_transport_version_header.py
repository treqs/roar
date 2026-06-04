"""Every GLaaS request carries an X-Roar-Version header so the server can identify the
client version (e.g. to nudge pre-0.3.4 clients, which omit artifact sizes on the
staged-link path). Absence of the header marks a legacy client."""

from __future__ import annotations

from unittest.mock import patch

from roar.integrations.glaas import transport


class _FakeResp:
    status = 200

    def read(self) -> bytes:
        return b'{"success": true}'

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def _capture_headers(**request_kwargs) -> dict[str, str]:
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured.update(dict(req.header_items()))
        return _FakeResp()

    with patch("urllib.request.urlopen", fake_urlopen):
        transport.request_json(**request_kwargs)
    # urllib normalizes header names to Capitalized-Dashes form.
    return {k.lower(): v for k, v in captured.items()}


def test_request_json_sends_version_header():
    headers = _capture_headers(
        base_url="https://glaas.example",
        method="POST",
        path="/api/v1/x",
        body={"a": 1},
        auth_header_factory=lambda *a: "Bearer tok",
    )
    assert headers.get("x-roar-version") == transport._roar_version()
    assert headers["x-roar-version"]  # non-empty


def test_version_header_sent_even_without_auth():
    headers = _capture_headers(
        base_url="https://glaas.example",
        method="GET",
        path="/api/v1/x",
        body=None,
        auth_header_factory=lambda *a: None,
    )
    assert headers.get("x-roar-version") == transport._roar_version()


def test_roar_version_falls_back_to_unknown(monkeypatch):
    monkeypatch.setattr(transport, "_ROAR_VERSION", None)
    with patch("importlib.metadata.version", side_effect=Exception("no dist")):
        assert transport._roar_version() == "unknown"
    monkeypatch.setattr(transport, "_ROAR_VERSION", None)  # reset cache for other tests
