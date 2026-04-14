from __future__ import annotations

from roar.publish_auth import PublishAuthContext, resolve_publish_creator_identity


def test_bearer_authenticated_treqs_context_prefers_treqs_subject() -> None:
    context = PublishAuthContext(
        access_token="token-123",
        scope_request={
            "owner_id": "owner-1",
            "owner_type": "organization",
            "visibility": "private",
        },
        auth_provider="treqs-device",
        user_sub="treqs-sub-123",
        db_user_id="glaas-user-123",
    )

    assert resolve_publish_creator_identity(context) == "treqs:user:treqs-sub-123"


def test_glaas_user_id_is_used_when_no_treqs_subject_is_available() -> None:
    context = PublishAuthContext(
        access_token="token-123",
        scope_request=None,
        auth_provider="glaas-local",
        user_sub=None,
        db_user_id="glaas-user-123",
    )

    assert resolve_publish_creator_identity(context) == "glaas:user:glaas-user-123"


def test_missing_authenticated_identity_resolves_to_anonymous() -> None:
    context = PublishAuthContext(
        access_token=None,
        scope_request=None,
        auth_provider=None,
        user_sub=None,
        db_user_id=None,
    )

    assert resolve_publish_creator_identity(context) == "anonymous"
