from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from roar.auth_store import AuthenticatedUser, AuthState
from roar.publish_auth import load_publish_auth_context


def _auth_state() -> AuthState:
    return AuthState(
        version=1,
        provider="treqs-cognito",
        issuer=None,
        client_id=None,
        access_token="access-token",
        refresh_token=None,
        refresh_expires_at=None,
        id_token=None,
        expires_at=None,
        user=AuthenticatedUser(
            sub="treqs-user-123",
            db_user_id="user-123",
            email="trevor@example.com",
            username="trevor",
        ),
    )


def test_public_publish_ignores_repo_binding_and_uses_authenticated_creator_identity(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".roar"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-123"\nowner_type = "organization"\nproject_id = "proj-456"\n',
        encoding="utf-8",
    )

    with (
        patch("roar.publish_auth.load_auth_state", return_value=None),
        patch(
            "roar.publish_auth._load_authenticated_creator_identity",
            return_value=("glaas:user:ssh-user-123", "ssh-user-123"),
        ),
    ):
        context = load_publish_auth_context(
            start_dir=tmp_path,
            allow_public_without_binding=True,
        )

    assert context.scope_request is None
    assert context.creator_identity == "glaas:user:ssh-user-123"
    assert context.db_user_id == "ssh-user-123"


def test_forced_anonymous_publish_ignores_auth_state_repo_binding_and_ssh_identity(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".roar"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-123"\nowner_type = "organization"\nproject_id = "proj-456"\n',
        encoding="utf-8",
    )

    with (
        patch("roar.publish_auth.load_auth_state") as load_auth_state,
        patch("roar.publish_auth._has_ssh_auth_credentials") as has_ssh_auth,
        patch("roar.publish_auth._load_authenticated_creator_identity") as load_identity,
    ):
        context = load_publish_auth_context(
            start_dir=tmp_path,
            allow_public_without_binding=True,
            force_anonymous=True,
        )

    load_auth_state.assert_not_called()
    has_ssh_auth.assert_not_called()
    load_identity.assert_not_called()
    assert context.scope_request is None
    assert context.access_token is None
    assert context.creator_identity is None
    assert context.db_user_id is None
    assert context.ssh_auth_available is False


def test_private_publish_without_binding_requires_bearer_auth(tmp_path: Path) -> None:
    with (
        patch("roar.publish_auth.load_auth_state", return_value=None),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
    ):
        try:
            load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)
        except RuntimeError as exc:
            message = str(exc)
            assert "requires GLaaS login" in message
            assert "--public" in message
        else:  # pragma: no cover - defensive guard
            raise AssertionError("expected missing auth to raise")


def test_private_publish_without_binding_uses_current_user_scope_request(
    tmp_path: Path,
) -> None:
    with (
        patch("roar.publish_auth.load_auth_state", return_value=_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
    ):
        context = load_publish_auth_context(
            start_dir=tmp_path,
            allow_public_without_binding=False,
        )

    assert context.access_token == "access-token"
    assert context.scope_request == {
        "owner_resolution": "current_user",
        "owner_type": "user",
        "visibility": "private",
    }


def test_public_scope_uses_current_user_public_scope_request(tmp_path: Path) -> None:
    config_dir = tmp_path / ".roar"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('[scope]\nmode = "public"\n', encoding="utf-8")

    with (
        patch("roar.publish_auth.load_auth_state", return_value=_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
    ):
        context = load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)

    assert context.scope_request == {
        "owner_resolution": "current_user",
        "owner_type": "user",
        "visibility": "public",
    }


def test_anonymous_scope_omits_scope_request(tmp_path: Path) -> None:
    config_dir = tmp_path / ".roar"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('[scope]\nmode = "anonymous"\n', encoding="utf-8")

    with (
        patch("roar.publish_auth.load_auth_state", return_value=_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
    ):
        context = load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)

    assert context.scope_request is None


def test_project_bound_private_publish_keeps_project_scope_request(tmp_path: Path) -> None:
    config_dir = tmp_path / ".roar"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-123"\nowner_type = "organization"\nproject_id = "proj-456"\n',
        encoding="utf-8",
    )

    with (
        patch("roar.publish_auth.load_auth_state", return_value=_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
    ):
        context = load_publish_auth_context(
            start_dir=tmp_path,
            allow_public_without_binding=False,
        )

    assert context.scope_request == {
        "owner_id": "owner-123",
        "owner_type": "organization",
        "project_id": "proj-456",
        "visibility": "private",
    }


def _expiring_auth_state() -> AuthState:
    return AuthState(
        version=1,
        provider="treqs-cognito",
        issuer=None,
        client_id=None,
        access_token="stale-token",
        refresh_token="refresh-token",
        refresh_expires_at=None,
        id_token=None,
        expires_at="2020-01-01T00:00:00Z",
        user=AuthenticatedUser(sub="u", db_user_id="user-123", email="t@e.com", username="trevor"),
    )


def test_publish_auth_refreshes_an_expiring_bearer(tmp_path: Path) -> None:
    with (
        patch("roar.publish_auth.load_auth_state", return_value=_expiring_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
        patch(
            "roar.glaas_client.ensure_fresh_access_token", return_value=("fresh-token", True)
        ) as ensure_fresh,
    ):
        context = load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)

    ensure_fresh.assert_called_once()
    assert context.access_token == "fresh-token"


def test_publish_auth_errors_to_login_when_bearer_unrefreshable_and_no_ssh(tmp_path: Path) -> None:
    from roar.glaas_client import GlaasClientError
    from roar.publish_auth import PublishAuthError

    with (
        patch("roar.publish_auth.load_auth_state", return_value=_expiring_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=False),
        patch(
            "roar.glaas_client.ensure_fresh_access_token",
            side_effect=GlaasClientError("Session expired. Run `roar login`."),
        ),
    ):
        try:
            load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)
            raise AssertionError("expected PublishAuthError")
        except PublishAuthError as exc:
            assert "roar login" in str(exc)


def test_publish_auth_falls_back_to_ssh_when_bearer_unrefreshable(tmp_path: Path) -> None:
    from roar.glaas_client import GlaasClientError

    binding = {"owner_id": "org-1", "owner_type": "organization", "project_id": "proj-1"}
    with (
        patch("roar.publish_auth.load_auth_state", return_value=_expiring_auth_state()),
        patch("roar.publish_auth._has_ssh_auth_credentials", return_value=True),
        patch("roar.publish_auth._load_repo_binding", return_value=binding),
        patch("roar.publish_auth.load_repo_scope", return_value=None),
        patch(
            "roar.glaas_client.ensure_fresh_access_token",
            side_effect=GlaasClientError("Session expired. Run `roar login`."),
        ),
    ):
        context = load_publish_auth_context(start_dir=tmp_path, allow_public_without_binding=False)

    # Bearer dropped; SSH auth carries the request (no error).
    assert context.access_token is None
    assert context.scope_request is not None
