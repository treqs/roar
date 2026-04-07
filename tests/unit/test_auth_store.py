from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import click
import pytest

from roar.auth_store import (
    auth_state_from_dict,
    auth_store_path,
    is_auth_state_expired,
    parse_auth_state_expiry,
    save_auth_state,
)


@pytest.fixture
def xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(path))
    return path


def test_save_auth_state_creates_file_with_secure_permissions(xdg_config_home: Path) -> None:
    path = save_auth_state(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "access-token",
            "user": {"sub": "user-123"},
        }
    )

    assert path == auth_store_path()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_parse_auth_state_expiry_accepts_utc_z_suffix() -> None:
    parsed = parse_auth_state_expiry("2030-01-01T00:00:00Z")

    assert parsed == datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_parse_auth_state_expiry_rejects_invalid_timestamp() -> None:
    with pytest.raises(click.ClickException, match="invalid expires_at timestamp"):
        parse_auth_state_expiry("not-a-timestamp")


def test_is_auth_state_expired_detects_stale_tokens() -> None:
    auth_state = auth_state_from_dict(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "access-token",
            "expires_at": "2020-01-01T00:00:00Z",
            "user": {"sub": "user-123"},
        }
    )

    assert is_auth_state_expired(auth_state, now=datetime(2030, 1, 1, tzinfo=timezone.utc)) is True


def test_is_auth_state_expired_keeps_non_expiring_tokens_valid() -> None:
    auth_state = auth_state_from_dict(
        {
            "version": 1,
            "provider": "treqs-device",
            "access_token": "access-token",
            "expires_at": None,
            "user": {"sub": "user-123"},
        }
    )

    assert is_auth_state_expired(auth_state, now=datetime(2030, 1, 1, tzinfo=timezone.utc)) is False
