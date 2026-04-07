from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import roar.glaas_client as treqs_client
from roar.auth_store import save_auth_state
from roar.glaas_client import GlaasClientError, fetch_access_context


@pytest.fixture
def xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(path))
    return path


def test_fetch_access_context_rejects_expired_stored_session(
    xdg_config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_auth_state(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "expired-token",
            "expires_at": "2020-01-01T00:00:00Z",
            "user": {"sub": "user-123"},
        }
    )

    monkeypatch.setattr(
        treqs_client,
        "fetch_access_context_via_auth_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call should not occur for expired tokens")),
    )

    with pytest.raises(GlaasClientError, match=r"Session expired\. Run `roar login`\."):
        fetch_access_context("https://api.treqs.ai")


def test_fetch_access_context_surfaces_sanitized_auth_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        treqs_client,
        "fetch_access_context_via_auth_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(treqs_client.GlaasAuthError("HTTP 401")),
    )

    with pytest.raises(GlaasClientError) as exc_info:
        fetch_access_context("https://api.glaas.ai", access_token="access-token")

    assert str(exc_info.value) == "Failed to fetch GLaaS auth access context: HTTP 401"


def test_fetch_access_context_refreshes_near_expiry_token(
    xdg_config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_auth_state(
        {
            "version": 1,
            "provider": "github",
            "issuer": "https://api.treqs.ai",
            "client_id": "roar-cli",
            "access_token": "stale-access-token",
            "refresh_token": "refresh-token",
            "refresh_expires_at": "2031-01-01T00:00:00Z",
            "expires_at": "2020-01-01T00:04:00Z",
            "user": {"sub": "user-123", "username": "trevor"},
        }
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2020, 1, 1, 0, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(treqs_client, "datetime", FrozenDateTime)
    monkeypatch.setattr(treqs_client, "resolve_auth_api_url", lambda: "https://api.glaas.ai")
    monkeypatch.setattr(
        treqs_client,
        "refresh_access_token",
        lambda api_url, refresh_token, client_id="roar-cli": SimpleNamespace(
            access_token="fresh-access-token",
            refresh_token="fresh-refresh-token",
            refresh_expires_at="2031-01-01T00:00:00Z",
            id_token=None,
            token_type="Bearer",
            expires_in=3600.0,
            provider="github",
            issuer="https://api.treqs.ai",
            client_id=client_id,
            raw_data={
                "access_context": {
                    "user": {
                        "id": "user-123",
                        "sub": "server-sub",
                        "email": "trevor@example.com",
                        "username": "trevor",
                    }
                }
            },
        ),
    )

    captured_auth_tokens: list[str] = []

    monkeypatch.setattr(
        treqs_client,
        "fetch_access_context_via_auth_api",
        lambda api_url, *, access_token: captured_auth_tokens.append(access_token) or {"user": {"id": "user-123"}},
    )

    payload = fetch_access_context("https://api.glaas.ai")

    assert payload == {"user": {"id": "user-123"}}
    assert captured_auth_tokens == ["fresh-access-token"]
    stored_auth = json.loads((xdg_config_home / "roar" / "auth.json").read_text(encoding="utf-8"))
    assert stored_auth["access_token"] == "fresh-access-token"
    assert stored_auth["refresh_token"] == "fresh-refresh-token"
    assert stored_auth["refresh_expires_at"] == "2031-01-01T00:00:00Z"
    assert stored_auth["user"]["db_user_id"] == "user-123"
    assert stored_auth["user"]["sub"] == "server-sub"


def test_fetch_access_context_reports_refresh_failure(
    xdg_config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=4)).replace(microsecond=0)
    save_auth_state(
        {
            "version": 1,
            "provider": "github",
            "issuer": "https://api.treqs.ai",
            "client_id": "roar-cli",
            "access_token": "stale-access-token",
            "refresh_token": "refresh-token",
            "refresh_expires_at": "2031-01-01T00:00:00Z",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "user": {"sub": "user-123", "username": "trevor"},
        }
    )

    monkeypatch.setattr(
        treqs_client,
        "refresh_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(treqs_client.GlaasAuthError("refresh rejected")),
    )

    with pytest.raises(GlaasClientError, match="Session refresh failed: refresh rejected"):
        fetch_access_context("https://api.treqs.ai")
