from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    ):
        try:
            build_sync_labels_summary(_sync_request(tmp_path, target="@1", entity_type="job"))
        except ValueError as exc:
            assert str(exc) == "No local user-managed labels to sync for @1."
        else:  # pragma: no cover - defensive assertion style
            raise AssertionError("Expected ValueError for missing user-managed labels")


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
