from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from roar.auth_store import save_auth_state
from roar.cli.commands.login import login
from roar.glaas_auth import (
    DeviceAuthorizationSession,
    DeviceTokenResponse,
    GlaasAuthError,
    open_browser,
    poll_device_token,
)


def test_login_defaults_to_device_flow_and_stores_enriched_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    opened_urls: list[str] = []

    monkeypatch.setattr(
        "roar.cli.commands.login.start_device_authorization",
        lambda api_url: DeviceAuthorizationSession(
            device_code="device-123",
            user_code="ABCD-EFGH",
            verification_uri="https://glaas.ai/login/device",
            verification_uri_complete="https://glaas.ai/login/device?user_code=ABCD-EFGH",
            expires_in=60.0,
            interval=0.0,
        ),
    )
    monkeypatch.setattr(
        "roar.cli.commands.login.open_browser",
        lambda url: opened_urls.append(url) or True,
    )
    monkeypatch.setattr(
        "roar.cli.commands.login.poll_device_token",
        lambda api_url, session: DeviceTokenResponse(
            access_token="device-access-token",
            refresh_token="refresh-token",
            refresh_expires_at="2031-01-01T00:00:00Z",
            id_token="id-token",
            token_type="Bearer",
            expires_in=1800.0,
            provider="treqs-device",
            issuer="https://api.treqs.ai",
            client_id="roar-cli",
            raw_data={},
        ),
    )
    monkeypatch.setattr(
        "roar.cli.commands.login.fetch_access_context_via_auth_api",
        lambda api_url, access_token: {
            "user": {
                "id": "user-123",
                "sub": "sub-123",
                "email": "trevor@example.com",
                "username": "trevor",
            }
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        login,
        [],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GLAAS_API_URL": "https://api.glaas.ai"},
    )

    assert result.exit_code == 0, result.output
    assert opened_urls == ["https://glaas.ai/login/device"]
    assert "Open this URL to approve login" in result.output
    assert "Enter this code on the site: ABCD-EFGH" in result.output
    assert "Opened browser to the GLaaS approval page. Enter the code shown above." in result.output
    assert "Stored auth state for trevor <trevor@example.com>" in result.output

    auth_path = tmp_path / "xdg" / "roar" / "auth.json"
    stored_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored_auth["access_token"] == "device-access-token"
    assert stored_auth["refresh_token"] == "refresh-token"
    assert stored_auth["user"]["db_user_id"] == "user-123"
    assert stored_auth["user"]["email"] == "trevor@example.com"
    assert stored_auth["provider"] == "treqs-device"


def test_login_device_flow_surfaces_poll_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "roar.cli.commands.login.start_device_authorization",
        lambda api_url: DeviceAuthorizationSession(
            device_code="device-123",
            user_code="ABCD-EFGH",
            verification_uri="https://glaas.ai/login/device",
            verification_uri_complete=None,
            expires_in=60.0,
            interval=0.0,
        ),
    )
    monkeypatch.setattr("roar.cli.commands.login.open_browser", lambda url: False)

    def _raise_poll_error(api_url: str, session: DeviceAuthorizationSession) -> DeviceTokenResponse:
        raise GlaasAuthError("Approval denied")

    monkeypatch.setattr("roar.cli.commands.login.poll_device_token", _raise_poll_error)

    runner = CliRunner()
    result = runner.invoke(
        login,
        [],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GLAAS_API_URL": "https://api.glaas.ai"},
    )

    assert result.exit_code != 0
    assert "Approval denied" in result.output
    assert not (tmp_path / "xdg" / "roar" / "auth.json").exists()


def test_open_browser_can_be_disabled_and_injected(monkeypatch) -> None:
    monkeypatch.setenv("ROAR_DISABLE_BROWSER_OPEN", "1")
    assert open_browser("https://glaas.ai/login/device", opener=lambda url: True) is False

    monkeypatch.delenv("ROAR_DISABLE_BROWSER_OPEN", raising=False)
    opened_urls: list[str] = []
    assert open_browser("https://glaas.ai/login/device", opener=lambda url: opened_urls.append(url) or True) is True
    assert opened_urls == ["https://glaas.ai/login/device"]


def test_poll_device_token_retries_pending_and_slow_down_then_succeeds(monkeypatch) -> None:
    responses = iter(
        [
            {"success": True, "data": {"status": "authorization_pending", "interval": 1}},
            GlaasAuthError("slow_down"),
            {
                "success": True,
                "data": {
                    "status": "approved",
                    "token": "device-access-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "expires_at": "2030-01-01T00:01:00Z",
                    "provider": "github",
                },
            },
        ]
    )
    sleeps: list[float] = []

    def _fake_request_json(url: str, *, method: str, payload: dict[str, object], headers=None):
        response = next(responses)
        if isinstance(response, dict):
            return response
        raise _make_response_error(400, {"error": {"code": "slow_down", "message": "slow down"}})

    import roar.glaas_auth as treqs_auth

    monkeypatch.setattr(treqs_auth, "_request_json", _fake_request_json)

    session = DeviceAuthorizationSession(
        device_code="device-123",
        user_code="ABCD-EFGH",
        verification_uri="https://glaas.ai/login/device",
        verification_uri_complete=None,
        expires_in=60.0,
        interval=1.0,
    )
    token_response = poll_device_token(
        "https://api.treqs.ai",
        session,
        sleep=lambda seconds: sleeps.append(seconds),
        time_fn=lambda: 0.0,
    )

    assert token_response.access_token == "device-access-token"
    assert sleeps == [1.0, 6.0]


def test_login_validates_token_file_against_backend(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "user": {"sub": "stale-sub", "username": "stale-user"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "roar.cli.commands.login.fetch_access_context_via_auth_api",
        lambda api_url, access_token: {
            "user": {
                "id": "user-123",
                "sub": "server-sub",
                "email": "trevor@example.com",
                "username": "trevor",
            }
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        login,
        ["--token-file", str(token_file), "--force"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GLAAS_API_URL": "https://api.glaas.ai"},
    )

    assert result.exit_code == 0, result.output
    stored_auth = json.loads((tmp_path / "xdg" / "roar" / "auth.json").read_text(encoding="utf-8"))
    assert stored_auth["user"]["db_user_id"] == "user-123"
    assert stored_auth["user"]["sub"] == "server-sub"
    assert stored_auth["user"]["username"] == "trevor"


def test_login_prompts_before_replacing_existing_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    save_auth_state(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "existing-token",
            "expires_at": "2030-01-01T00:00:00Z",
            "user": {"sub": "existing-sub", "username": "existing-user"},
        }
    )

    token_file = tmp_path / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "user": {"sub": "stale-sub", "username": "stale-user"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "roar.cli.commands.login.fetch_access_context_via_auth_api",
        lambda api_url, access_token: {
            "user": {
                "id": "user-123",
                "sub": "server-sub",
                "email": "trevor@example.com",
                "username": "trevor",
            }
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        login,
        ["--token-file", str(token_file)],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GLAAS_API_URL": "https://api.glaas.ai"},
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Already logged in as existing-user." in result.output
    assert "Login cancelled; existing session preserved." in result.output


def test_login_force_replaces_existing_session_without_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    save_auth_state(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "existing-token",
            "expires_at": "2030-01-01T00:00:00Z",
            "user": {"sub": "existing-sub", "username": "existing-user"},
        }
    )

    token_file = tmp_path / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "access_token": "replacement-token",
                "refresh_token": "replacement-refresh-token",
                "user": {"sub": "stale-sub", "username": "stale-user"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "roar.cli.commands.login.fetch_access_context_via_auth_api",
        lambda api_url, access_token: {
            "user": {
                "id": "user-123",
                "sub": "server-sub",
                "email": "trevor@example.com",
                "username": "trevor",
            }
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        login,
        ["--token-file", str(token_file), "--force"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GLAAS_API_URL": "https://api.glaas.ai"},
    )

    assert result.exit_code == 0, result.output
    assert "Replace existing session?" not in result.output
    stored_auth = json.loads((tmp_path / "xdg" / "roar" / "auth.json").read_text(encoding="utf-8"))
    assert stored_auth["access_token"] == "replacement-token"


def _make_response_error(status: int, payload: dict[str, object]):
    import roar.glaas_auth as treqs_auth

    return treqs_auth._GlaasApiResponseError(status, payload)
