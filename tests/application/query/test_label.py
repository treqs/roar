from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call, patch

import click

from roar.application.labels import ReconcileTargetSync
from roar.application.query import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
    LabelSyncRequest,
    LabelUnsetRequest,
)
from roar.application.query.label import (
    build_copy_labels_summary,
    build_label_history_summary,
    build_set_labels_summary,
    build_show_labels_summary,
    build_sync_labels_summary,
    build_unset_labels_summary,
)
from roar.publish_auth import PublishAuthError


def _set_request(tmp_path: Path, **overrides) -> LabelSetRequest:
    return LabelSetRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
        pairs=overrides.pop("pairs", ("stage=gold", "owner=ml")),
        **overrides,
    )


def _unset_request(tmp_path: Path, **overrides) -> LabelUnsetRequest:
    return LabelUnsetRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
        keys=overrides.pop("keys", ("stage",)),
        **overrides,
    )


def _copy_request(tmp_path: Path, **overrides) -> LabelCopyRequest:
    return LabelCopyRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        source_entity_type=overrides.pop("source_entity_type", "artifact"),
        source_target=overrides.pop("source_target", "processed.csv"),
        destination_entity_type=overrides.pop("destination_entity_type", "artifact"),
        destination_target=overrides.pop("destination_target", "model.pkl"),
        **overrides,
    )


def _show_request(tmp_path: Path, **overrides) -> LabelShowRequest:
    return LabelShowRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
        **overrides,
    )


def _history_request(tmp_path: Path, **overrides) -> LabelHistoryRequest:
    return LabelHistoryRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
        **overrides,
    )


def _sync_request(tmp_path: Path, **overrides) -> LabelSyncRequest:
    return LabelSyncRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
        dry_run=overrides.pop("dry_run", False),
        output_json=overrides.pop("output_json", False),
        **overrides,
    )


def test_build_set_labels_summary_returns_only_labels_changed_by_the_patch(
    tmp_path: Path,
) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {
        "owner": "ml",
        "roar": {"operation": {"kind": "run"}},
    }
    service.set_metadata.return_value = MagicMock(
        changed=True,
        version=2,
        metadata={
            "owner": "ml",
            "stage": "gold",
            "roar": {"operation": {"kind": "run"}},
        },
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_set_labels_summary(_set_request(tmp_path))

    assert summary.heading == "Updated labels (version 2):"
    assert [(entry.key, entry.display_value) for entry in summary.entries] == [
        ("stage", "gold"),
    ]
    assert summary.render() == "Updated labels (version 2):\n  stage=gold"


def test_build_set_labels_summary_reports_no_label_changes_for_noop_updates(
    tmp_path: Path,
) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml", "stage": "gold"}
    service.set_metadata.return_value = MagicMock(
        changed=False,
        version=2,
        metadata={"owner": "ml", "stage": "gold"},
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_set_labels_summary(_set_request(tmp_path))

    assert summary.heading == "Labels unchanged (version 2):"
    assert summary.entries == []
    assert summary.render() == "Labels unchanged (version 2):\n  No label changes."


def test_build_unset_labels_summary_returns_removed_labels(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml", "stage": "gold"}
    service.unset_metadata.return_value = MagicMock(
        changed=True,
        version=3,
        metadata={"owner": "ml"},
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_unset_labels_summary(_unset_request(tmp_path))

    assert summary.heading == "Removed labels (version 3):"
    assert summary.render() == "Removed labels (version 3):\n  stage=gold"


def test_build_copy_labels_summary_preserves_no_change_heading(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.side_effect = [object(), object()]
    service.copy_metadata.return_value = MagicMock(
        changed=False,
        version=3,
        metadata={"owner": "ml"},
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_copy_labels_summary(_copy_request(tmp_path))

    assert summary.heading == "Copy made no changes (version 3):"
    assert summary.render() == "Copy made no changes (version 3):\n  owner=ml"


def test_build_show_labels_summary_renders_no_labels_when_empty(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {}

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_show_labels_summary(_show_request(tmp_path))

    assert summary.heading is None
    assert summary.entries == []
    assert summary.render() == "No labels."


def test_build_sync_labels_summary_reconciles_target_labels(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    resolved_target = object()
    service.resolve_target.return_value = resolved_target
    service.current_metadata.return_value = {"owner": "ml", "stage": "gold"}
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token",
        ssh_auth_available=False,
        scope_request=None,
    )
    client.reconcile_labels.return_value = (
        {
            "processed": 1,
            "created": 0,
            "updated": 1,
            "noops": 0,
            "conflicts": [],
            "results": [
                {
                    "entityType": "artifact",
                    "artifactHash": "a" * 64,
                    "action": "updated",
                    "version": 2,
                }
            ],
        },
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=(
                "s" * 64,
                [
                    {
                        "entity_type": "artifact",
                        "session_hash": "s" * 64,
                        "artifact_hash": "a" * 64,
                        "metadata": {"owner": "ml", "stage": "gold"},
                    }
                ],
                [
                    ReconcileTargetSync(
                        entity_type="artifact",
                        session_hash="s" * 64,
                        job_uid=None,
                        artifact_hash="a" * 64,
                        label_ids=[41],
                        deleted_keys=[],
                    )
                ],
            ),
        ) as build_payload,
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
    ):
        summary = build_sync_labels_summary(_sync_request(tmp_path))

    build_payload.assert_called_once_with(
        db_ctx,
        roar_dir=tmp_path / ".roar",
        target=resolved_target,
        metadata={"owner": "ml", "stage": "gold"},
    )
    client.reconcile_labels.assert_called_once_with(
        {
            "session_hash": "s" * 64,
            "mode": "sync_user_labels",
            "dry_run": False,
            "prune": False,
            "labels": [
                {
                    "entity_type": "artifact",
                    "session_hash": "s" * 64,
                    "artifact_hash": "a" * 64,
                    "metadata": {"owner": "ml", "stage": "gold"},
                }
            ],
        }
    )
    assert not isinstance(summary, str)
    assert summary.heading == "Synced remote labels: processed=1 created=0 updated=1 noops=0"
    assert summary.render() == (
        "Synced remote labels: processed=1 created=0 updated=1 noops=0\n"
        f"  artifact:{'a' * 64}=updated version 2"
    )


def test_build_sync_labels_summary_falls_back_to_public_auth_without_repo_binding(
    tmp_path: Path,
) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    client = MagicMock()
    public_auth = SimpleNamespace(
        access_token="test-token",
        ssh_auth_available=False,
        scope_request=None,
    )
    client.publish_auth = public_auth
    client.reconcile_labels.return_value = (
        {"processed": 1, "created": 0, "updated": 0, "noops": 1, "results": []},
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_current_lineage",
            return_value=(
                "s" * 64,
                [{"entity_type": "dag", "session_hash": "s" * 64, "metadata": {"phase": "gold"}}],
                [
                    ReconcileTargetSync(
                        entity_type="dag",
                        session_hash="s" * 64,
                        job_uid=None,
                        artifact_hash=None,
                        label_ids=[7],
                        deleted_keys=[],
                    )
                ],
            ),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            side_effect=[
                PublishAuthError("No GLaaS repo binding found for this publish."),
                public_auth,
            ],
        ) as load_auth,
        patch("roar.application.query.label.GlaasClient", return_value=client) as client_class,
    ):
        summary = build_sync_labels_summary(
            LabelSyncRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                dry_run=False,
                output_json=False,
            )
        )

    load_auth.assert_has_calls(
        [
            call(tmp_path, allow_public_without_binding=False),
            call(tmp_path, allow_public_without_binding=True),
        ]
    )
    client_class.assert_called_once_with(
        start_dir=str(tmp_path),
        publish_auth=public_auth,
        allow_public_without_binding=True,
    )
    client.reconcile_labels.assert_called_once()
    assert not isinstance(summary, str)
    assert summary.heading == "Synced remote labels: processed=1 created=0 updated=0 noops=1"


def test_build_sync_labels_summary_preserves_reconcile_application_404s(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token",
        ssh_auth_available=False,
        scope_request=None,
    )
    client.reconcile_labels.return_value = (None, "HTTP 404: Session not found: s")

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_current_lineage",
            return_value=(
                "s" * 64,
                [{"entity_type": "dag", "session_hash": "s" * 64, "metadata": {"phase": "gold"}}],
                [],
            ),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
    ):
        try:
            build_sync_labels_summary(
                LabelSyncRequest(
                    roar_dir=tmp_path / ".roar",
                    cwd=tmp_path,
                    dry_run=False,
                    output_json=False,
                )
            )
        except ValueError as exc:
            assert str(exc) == "Remote label sync failed: HTTP 404: Session not found: s"
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for reconcile application 404")


def test_build_sync_labels_summary_supports_json_dry_run(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token",
        ssh_auth_available=False,
        scope_request=None,
    )
    client.reconcile_labels.return_value = (
        {"dryRun": True, "processed": 1, "created": 1, "updated": 0, "noops": 0},
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_current_lineage",
            return_value=(
                "s" * 64,
                [{"entity_type": "dag", "session_hash": "s" * 64, "metadata": {"phase": "gold"}}],
                [
                    ReconcileTargetSync(
                        entity_type="dag",
                        session_hash="s" * 64,
                        job_uid=None,
                        artifact_hash=None,
                        label_ids=[9],
                        deleted_keys=[],
                    )
                ],
            ),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
    ):
        rendered = build_sync_labels_summary(
            LabelSyncRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                dry_run=True,
                output_json=True,
            )
        )

    assert isinstance(rendered, str)
    assert json.loads(rendered) == {
        "dryRun": True,
        "processed": 1,
        "created": 1,
        "updated": 0,
        "noops": 0,
    }
    client.reconcile_labels.assert_called_once_with(
        {
            "session_hash": "s" * 64,
            "mode": "sync_user_labels",
            "dry_run": True,
            "prune": False,
            "labels": [
                {"entity_type": "dag", "session_hash": "s" * 64, "metadata": {"phase": "gold"}}
            ],
        }
    )


def test_build_sync_labels_summary_rejects_missing_user_managed_labels(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"roar": {"operation": {"kind": "run"}}}

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        # System-only metadata and no pending local deletions → nothing to sync.
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=("s" * 64, [], []),
        ),
    ):
        try:
            build_sync_labels_summary(_sync_request(tmp_path, target="@1", entity_type="job"))
        except ValueError as exc:
            assert str(exc) == "No local user-managed labels or label deletions to sync for @1."
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for missing user-managed labels")


def _deletion_target_payload() -> tuple[str, list[dict], list[ReconcileTargetSync]]:
    return (
        "s" * 64,
        [
            {
                "entity_type": "artifact",
                "session_hash": "s" * 64,
                "artifact_hash": "a" * 64,
                "metadata": {"owner": "ml"},
                "deleted_keys": ["stage"],
            }
        ],
        [
            ReconcileTargetSync(
                entity_type="artifact",
                session_hash="s" * 64,
                job_uid=None,
                artifact_hash="a" * 64,
                label_ids=[41],
                deleted_keys=["stage"],
            )
        ],
    )


def test_build_sync_labels_summary_prompts_and_aborts_when_deletion_is_declined(
    tmp_path: Path,
) -> None:
    """Task B: declining the deletion-confirmation prompt aborts before any
    network call is made — nothing is sent to GLaaS."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml"}

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=_deletion_target_payload(),
        ),
        patch("roar.application.query.label.GlaasClient") as client_class,
        patch("roar.application.query.label.click.confirm", return_value=False) as confirm,
    ):
        try:
            build_sync_labels_summary(_sync_request(tmp_path))
        except SystemExit as exc:
            assert exc.code == 1
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected SystemExit when the deletion prompt is declined")

    confirm.assert_called_once()
    client_class.assert_not_called()


def test_build_sync_labels_summary_deletion_prompt_noninteractive_gives_clear_error(
    tmp_path: Path,
) -> None:
    """click.confirm() raises click.Abort on EOF (closed/absent stdin) — the shape
    a workflow-orchestrated subprocess with no terminal attached actually hits,
    distinct from the simulated-decline test above (return_value=False models a
    real "n" keystroke). This must surface as an actionable ClickException, not
    Click's generic "Aborted!", and must not touch the network either way."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml"}

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=_deletion_target_payload(),
        ),
        patch("roar.application.query.label.GlaasClient") as client_class,
        patch("roar.application.query.label.click.confirm", side_effect=click.Abort()) as confirm,
    ):
        try:
            build_sync_labels_summary(_sync_request(tmp_path))
        except click.ClickException as exc:
            assert "non-interactive session" in str(exc)
            assert "label sync -y" in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ClickException on a non-interactive EOF")

    confirm.assert_called_once()
    client_class.assert_not_called()


def test_build_sync_labels_summary_prompts_and_proceeds_when_deletion_is_accepted(
    tmp_path: Path,
) -> None:
    """Task B: accepting the prompt proceeds with the sync as before."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml"}
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token", ssh_auth_available=False, scope_request=None
    )
    client.reconcile_labels.return_value = (
        {
            "processed": 1,
            "created": 0,
            "updated": 1,
            "noops": 0,
            "results": [
                {
                    "entityType": "artifact",
                    "sessionHash": "s" * 64,
                    "artifactHash": "a" * 64,
                    "action": "updated",
                    "version": 2,
                    "deletedKeys": ["stage"],
                }
            ],
        },
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=_deletion_target_payload(),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
        patch("roar.application.query.label.click.confirm", return_value=True) as confirm,
    ):
        summary = build_sync_labels_summary(_sync_request(tmp_path))

    confirm.assert_called_once()
    client.reconcile_labels.assert_called_once()
    assert not isinstance(summary, str)
    db_ctx.labels.mark_synced.assert_called_once_with([41], ANY)


def test_build_sync_labels_summary_skip_confirmation_bypasses_prompt(tmp_path: Path) -> None:
    """Task B: -y/--yes (skip_confirmation) never prompts."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml"}
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token", ssh_auth_available=False, scope_request=None
    )
    client.reconcile_labels.return_value = (
        {"processed": 1, "created": 0, "updated": 1, "noops": 0, "results": []},
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=_deletion_target_payload(),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
        patch("roar.application.query.label.click.confirm") as confirm,
    ):
        build_sync_labels_summary(_sync_request(tmp_path, skip_confirmation=True))

    confirm.assert_not_called()
    client.reconcile_labels.assert_called_once()


def test_build_sync_labels_summary_dry_run_bypasses_prompt(tmp_path: Path) -> None:
    """Task B: --dry-run never prompts, even with pending deletions."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.current_metadata.return_value = {"owner": "ml"}
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token", ssh_auth_available=False, scope_request=None
    )
    client.reconcile_labels.return_value = (
        {"processed": 1, "created": 0, "updated": 0, "noops": 0, "dryRun": True, "results": []},
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_reconcile_payload_for_target",
            return_value=_deletion_target_payload(),
        ),
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
        patch("roar.application.query.label.click.confirm") as confirm,
    ):
        build_sync_labels_summary(_sync_request(tmp_path, dry_run=True))

    confirm.assert_not_called()
    client.reconcile_labels.assert_called_once()
    db_ctx.labels.mark_synced.assert_not_called()


def test_mark_labels_synced_confirming_deletions_skips_unconfirmed_targets(
    tmp_path: Path, capsys
) -> None:
    """Task A: an old server that echoes back an empty deletedKeys list for a
    target whose payload requested deletions must not have its baseline
    advanced, and the user should see a clear warning. Other targets in the
    same sync (no deletions requested, or confirmed) still advance."""
    from roar.application.query.label import _mark_labels_synced_confirming_deletions

    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None

    sync_targets = [
        ReconcileTargetSync(
            entity_type="artifact",
            session_hash="s" * 64,
            job_uid=None,
            artifact_hash="a" * 64,
            label_ids=[41],
            deleted_keys=["stage"],
        ),
        ReconcileTargetSync(
            entity_type="dag",
            session_hash="s" * 64,
            job_uid=None,
            artifact_hash=None,
            label_ids=[7],
            deleted_keys=[],
        ),
    ]
    # Old server: HTTP 200, reconcile "succeeds", but the artifact row's
    # deletedKeys is empty even though `stage` deletion was requested.
    result = {
        "results": [
            {
                "entityType": "artifact",
                "sessionHash": "s" * 64,
                "artifactHash": "a" * 64,
                "action": "noop",
                "deletedKeys": [],
            },
            {
                "entityType": "dag",
                "sessionHash": "s" * 64,
                "action": "updated",
                "deletedKeys": [],
            },
        ]
    }

    with patch("roar.application.query.label.create_database_context", return_value=db_ctx):
        _mark_labels_synced_confirming_deletions(tmp_path / ".roar", sync_targets, result)

    db_ctx.labels.mark_synced.assert_called_once_with([7], ANY)
    captured = capsys.readouterr()
    assert "Warning: sent 1 label deletion(s) for" in captured.err
    assert "a" * 64 in captured.err
    assert "did not confirm they were applied" in captured.err
    assert "Local state was not marked as synced" in captured.err


def test_mark_labels_synced_confirming_deletions_advances_confirmed_targets(
    tmp_path: Path, capsys
) -> None:
    """Task A: when the response confirms the exact deleted keys, the
    baseline advances normally and no warning is printed."""
    from roar.application.query.label import _mark_labels_synced_confirming_deletions

    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None

    sync_targets = [
        ReconcileTargetSync(
            entity_type="artifact",
            session_hash="s" * 64,
            job_uid=None,
            artifact_hash="a" * 64,
            label_ids=[41],
            deleted_keys=["stage"],
        )
    ]
    result = {
        "results": [
            {
                "entityType": "artifact",
                "sessionHash": "s" * 64,
                "artifactHash": "a" * 64,
                "action": "updated",
                "deletedKeys": ["stage"],
            }
        ]
    }

    with patch("roar.application.query.label.create_database_context", return_value=db_ctx):
        _mark_labels_synced_confirming_deletions(tmp_path / ".roar", sync_targets, result)

    db_ctx.labels.mark_synced.assert_called_once_with([41], ANY)
    assert capsys.readouterr().err == ""


def test_build_label_history_summary_returns_versioned_entries(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.history.return_value = [
        {"version": 1, "metadata": {"owner": "ml", "stage": "raw"}},
        {"version": 2, "metadata": {"owner": "ml", "stage": "gold"}},
    ]

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_label_history_summary(_history_request(tmp_path))

    assert [version.version for version in summary.versions] == [1, 2]
    assert [(entry.key, entry.display_value) for entry in summary.versions[0].entries] == [
        ("owner", "ml"),
        ("stage", "raw"),
    ]
    assert (
        summary.render()
        == "Version 1:\n  owner=ml\n  stage=raw\n\nVersion 2:\n  owner=ml\n  stage=gold"
    )


def _mock_remote_client(
    *,
    current: tuple[dict | None, str | None] = (None, "HTTP 404: Label not found"),
    reconcile: tuple[dict | None, str | None] | None = None,
    history: tuple[dict | None, str | None] | None = None,
    scope_request: dict | None = None,
) -> MagicMock:
    client = MagicMock()
    client.publish_auth = SimpleNamespace(
        access_token="test-token",
        ssh_auth_available=False,
        scope_request=scope_request,
    )
    client.get_current_labels.return_value = current
    if reconcile is not None:
        client.reconcile_labels.return_value = reconcile
    if history is not None:
        client.get_label_history.return_value = history
    return client


def _remote_patches(client: MagicMock):
    return (
        patch(
            "roar.application.query.label.load_publish_auth_context",
            return_value=client.publish_auth,
        ),
        patch("roar.application.query.label.GlaasClient", return_value=client),
    )


def test_remote_target_params_validates_identifiers() -> None:
    from roar.application.query.label import _remote_target_params

    session = "a" * 64
    assert _remote_target_params("dag", session.upper(), None) == {
        "entity_type": "dag",
        "session_hash": session,
    }
    assert _remote_target_params("job", "step-1", session) == {
        "entity_type": "job",
        "session_hash": session,
        "job_uid": "step-1",
    }
    assert _remote_target_params("composite", "ABCD1234", None) == {
        "entity_type": "artifact",
        "artifact_hash": "abcd1234",
    }
    assert _remote_target_params("artifact", "b" * 64, session) == {
        "entity_type": "artifact",
        "artifact_hash": "b" * 64,
        "session_hash": session,
    }

    for entity_type, target, session_hash, message in [
        ("dag", "abc123", None, "64-character"),
        ("job", "step-1", None, "--session"),
        ("job", "", session, "job uid"),
        ("artifact", "xyz", None, "8 hex"),
        ("dag", "a" * 64, "b" * 64, "conflicts"),
        ("artifact", "b" * 64, "not-a-hash", "--session must be"),
    ]:
        try:
            _remote_target_params(entity_type, target, session_hash)
        except ValueError as exc:
            assert message in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError(f"Expected ValueError for {entity_type}:{target}")


def test_remote_set_sends_patch_with_base_version(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    session = "a" * 64
    client = _mock_remote_client(
        current=({"version": 3, "metadata": {"team": "nlp"}, "canEdit": True}, None),
        reconcile=(
            {
                "processed": 1,
                "created": 0,
                "updated": 1,
                "noops": 0,
                "results": [
                    {"entityType": "dag", "sessionHash": session, "action": "updated", "version": 4}
                ],
            },
            None,
        ),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        summary = build_remote_set_labels_summary(
            RemoteLabelSetRequest(
                cwd=tmp_path,
                entity_type="dag",
                target=session,
                pairs=("team=cv", "priority=2"),
            )
        )

    client.reconcile_labels.assert_called_once_with(
        {
            "session_hash": session,
            "mode": "sync_user_labels",
            "dry_run": False,
            "prune": False,
            "labels": [
                {
                    "entity_type": "dag",
                    "session_hash": session,
                    "metadata": {"priority": 2, "team": "cv"},
                    "base_version": 3,
                }
            ],
        }
    )
    assert summary.heading == "Updated remote labels: processed=1 created=0 updated=1 noops=0"


def test_remote_set_creates_when_no_labels_exist(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    session = "a" * 64
    client = _mock_remote_client(
        current=(None, "HTTP 404: Label not found"),
        reconcile=(
            {"processed": 1, "created": 1, "updated": 0, "noops": 0, "results": []},
            None,
        ),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        build_remote_set_labels_summary(
            RemoteLabelSetRequest(
                cwd=tmp_path, entity_type="dag", target=session, pairs=("team=cv",)
            )
        )

    sent = client.reconcile_labels.call_args.args[0]["labels"][0]
    assert sent["base_version"] == 0
    assert sent["metadata"] == {"team": "cv"}


def test_remote_set_rejects_reserved_keys(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    try:
        build_remote_set_labels_summary(
            RemoteLabelSetRequest(
                cwd=tmp_path,
                entity_type="dag",
                target="a" * 64,
                pairs=("roar.pipeline=hijack",),
            )
        )
    except ValueError as exc:
        assert "Reserved label keys" in str(exc)
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("Expected ValueError for reserved keys")


def test_remote_unset_sends_deleted_keys(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelUnsetRequest
    from roar.application.query.label import build_remote_unset_labels_summary

    session = "a" * 64
    artifact = "b" * 64
    client = _mock_remote_client(
        current=(
            {
                "version": 2,
                "metadata": {"stage": "gold", "team": "nlp"},
                "sessionHash": session,
                "artifactHash": artifact,
            },
            None,
        ),
        reconcile=(
            {
                "processed": 1,
                "created": 0,
                "updated": 1,
                "noops": 0,
                "deletedKeys": 1,
                "results": [
                    {
                        "entityType": "artifact",
                        "artifactHash": artifact,
                        "action": "updated",
                        "version": 3,
                        "deletedKeys": ["stage"],
                    }
                ],
            },
            None,
        ),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        summary = build_remote_unset_labels_summary(
            RemoteLabelUnsetRequest(
                cwd=tmp_path,
                entity_type="artifact",
                target=artifact[:12],
                keys=("stage",),
            )
        )

    sent = client.reconcile_labels.call_args.args[0]
    assert sent["session_hash"] == session
    assert sent["labels"][0] == {
        "entity_type": "artifact",
        "session_hash": session,
        "artifact_hash": artifact,
        "metadata": {},
        "deleted_keys": ["stage"],
        "base_version": 2,
    }
    assert "deleted_keys=1" in summary.heading
    assert "(deleted: stage)" in summary.render()


def test_remote_unset_requires_existing_labels(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelUnsetRequest
    from roar.application.query.label import build_remote_unset_labels_summary

    client = _mock_remote_client(current=(None, "HTTP 404: Label not found"))

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_unset_labels_summary(
                RemoteLabelUnsetRequest(
                    cwd=tmp_path, entity_type="dag", target="a" * 64, keys=("stage",)
                )
            )
        except ValueError as exc:
            assert "No remote labels found" in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for missing labels")


def test_remote_show_renders_version_and_editability(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelShowRequest
    from roar.application.query.label import build_remote_show_labels_summary

    client = _mock_remote_client(
        current=({"version": 5, "metadata": {"team": "nlp"}, "canEdit": False}, None)
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        summary = build_remote_show_labels_summary(
            RemoteLabelShowRequest(cwd=tmp_path, entity_type="dag", target="a" * 64)
        )

    assert summary.heading == "Remote labels (version 5, read-only):"
    assert summary.render() == "Remote labels (version 5, read-only):\n  team=nlp"


def test_remote_history_renders_versions(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelHistoryRequest
    from roar.application.query.label import build_remote_label_history_summary

    client = _mock_remote_client(
        history=(
            {
                "labels": [
                    {"version": 1, "metadata": {"stage": "gold", "team": "nlp"}},
                    {"version": 2, "metadata": {"team": "nlp"}},
                ]
            },
            None,
        )
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        summary = build_remote_label_history_summary(
            RemoteLabelHistoryRequest(cwd=tmp_path, entity_type="dag", target="a" * 64)
        )

    assert [version.version for version in summary.versions] == [1, 2]


def test_remote_edit_maps_permission_errors(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    client = _mock_remote_client(
        current=({"version": 1, "metadata": {}}, None),
        reconcile=(None, "HTTP 403: Public label writes require the lineage creator"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_set_labels_summary(
                RemoteLabelSetRequest(
                    cwd=tmp_path, entity_type="dag", target="a" * 64, pairs=("team=cv",)
                )
            )
        except ValueError as exc:
            assert "Remote label edit was denied" in str(exc)
            assert "write access to the lineage's scope" in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for 403")


def test_remote_set_resolves_artifact_session_when_unlabeled(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    session = "a" * 64
    artifact = "b" * 64
    client = _mock_remote_client(
        current=(None, "HTTP 404: Label not found"),
        reconcile=(
            {"processed": 1, "created": 1, "updated": 0, "noops": 0, "results": []},
            None,
        ),
    )
    client.get_artifact.return_value = {"hash": artifact, "originalSessionHash": session}

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        build_remote_set_labels_summary(
            RemoteLabelSetRequest(
                cwd=tmp_path,
                entity_type="artifact",
                target=artifact[:12],
                pairs=("stage=gold",),
            )
        )

    client.get_artifact.assert_called_once_with(artifact[:12])
    sent = client.reconcile_labels.call_args.args[0]
    assert sent["session_hash"] == session
    assert sent["labels"][0]["artifact_hash"] == artifact
    assert sent["labels"][0]["session_hash"] == session


# Task C: GET /api/v1/labels/current and /api/v1/labels/history 404s are
# ambiguous — "no labels yet" on an upgraded server vs. "route doesn't exist"
# on an old one. Only the latter should surface as a clear, actionable error;
# the former must keep behaving as "no labels yet" (covered by the
# `current=(None, "HTTP 404: Label not found")` tests above, which are
# unaffected by this change since that message never contains "Endpoint not
# found").


def test_remote_set_raises_clear_error_when_get_current_route_is_missing(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    client = _mock_remote_client(
        current=(None, "HTTP 404: Endpoint not found"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_set_labels_summary(
                RemoteLabelSetRequest(
                    cwd=tmp_path, entity_type="dag", target="a" * 64, pairs=("team=cv",)
                )
            )
        except ValueError as exc:
            assert "GET /api/v1/labels/current" in str(exc)
            assert "may not be upgraded yet" in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for a missing GET route")
    client.reconcile_labels.assert_not_called()


def test_remote_unset_raises_clear_error_when_get_current_route_is_missing(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelUnsetRequest
    from roar.application.query.label import build_remote_unset_labels_summary

    client = _mock_remote_client(
        current=(None, "HTTP 404: Endpoint not found"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_unset_labels_summary(
                RemoteLabelUnsetRequest(
                    cwd=tmp_path, entity_type="dag", target="a" * 64, keys=("stage",)
                )
            )
        except ValueError as exc:
            assert "GET /api/v1/labels/current" in str(exc)
            assert "may not be upgraded yet" in str(exc)
            # Must NOT be misread as the (misleading, for this case) "no
            # remote labels found" message.
            assert "No remote labels found" not in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for a missing GET route")


def test_remote_show_raises_clear_error_when_get_current_route_is_missing(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelShowRequest
    from roar.application.query.label import build_remote_show_labels_summary

    client = _mock_remote_client(
        current=(None, "HTTP 404: Endpoint not found"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_show_labels_summary(
                RemoteLabelShowRequest(cwd=tmp_path, entity_type="dag", target="a" * 64)
            )
        except ValueError as exc:
            assert "GET /api/v1/labels/current" in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for a missing GET route")


def test_remote_history_raises_clear_error_when_get_history_route_is_missing(
    tmp_path: Path,
) -> None:
    from roar.application.query import RemoteLabelHistoryRequest
    from roar.application.query.label import build_remote_label_history_summary

    client = _mock_remote_client(
        history=(None, "HTTP 404: Endpoint not found"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_label_history_summary(
                RemoteLabelHistoryRequest(cwd=tmp_path, entity_type="dag", target="a" * 64)
            )
        except ValueError as exc:
            assert "GET /api/v1/labels/history" in str(exc)
            assert "may not be upgraded yet" in str(exc)
            assert "No remote labels found" not in str(exc)
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for a missing GET route")


def test_remote_history_still_treats_genuine_404_as_no_labels(tmp_path: Path) -> None:
    """The app-level 'no labels yet' 404 (a different message than 'Endpoint
    not found') must keep behaving as before for history, too."""
    from roar.application.query import RemoteLabelHistoryRequest
    from roar.application.query.label import build_remote_label_history_summary

    client = _mock_remote_client(
        history=(None, "HTTP 404: Label not found"),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_label_history_summary(
                RemoteLabelHistoryRequest(cwd=tmp_path, entity_type="dag", target="a" * 64)
            )
        except ValueError as exc:
            assert str(exc) == "No remote labels found for the target."
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for missing labels")


# Task D: the --remote edit path's optimistic locking (base_version) maps a
# 409 conflict to a clear, actionable message rather than a raw traceback.


def test_remote_edit_maps_conflict_errors(tmp_path: Path) -> None:
    from roar.application.query import RemoteLabelSetRequest
    from roar.application.query.label import build_remote_set_labels_summary

    client = _mock_remote_client(
        current=({"version": 4, "metadata": {"team": "nlp"}}, None),
        reconcile=(
            None,
            "HTTP 409: Label was modified concurrently (expected version 4, found 5)",
        ),
    )

    auth_patch, client_patch = _remote_patches(client)
    with auth_patch, client_patch:
        try:
            build_remote_set_labels_summary(
                RemoteLabelSetRequest(
                    cwd=tmp_path, entity_type="dag", target="a" * 64, pairs=("team=cv",)
                )
            )
        except ValueError as exc:
            message = str(exc)
            assert "conflicted with a concurrent edit" in message
            assert "Retry" in message
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for 409")
