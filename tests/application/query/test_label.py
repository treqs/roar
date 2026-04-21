from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from roar.application.query import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelPushRequest,
    LabelSetRequest,
    LabelShowRequest,
)
from roar.application.query.label import (
    build_copy_labels_summary,
    build_label_history_summary,
    build_push_labels_summary,
    build_set_labels_summary,
    build_show_labels_summary,
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


def _push_request(tmp_path: Path, **overrides) -> LabelPushRequest:
    return LabelPushRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        entity_type=overrides.pop("entity_type", "artifact"),
        target=overrides.pop("target", "processed.csv"),
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


def test_build_push_labels_summary_returns_remote_versioned_summary(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    resolved_target = object()
    service.resolve_target.return_value = resolved_target
    service.current_metadata.return_value = {"owner": "ml", "stage": "gold"}
    client = MagicMock()
    client.patch_current_label.return_value = (
        {
            "version": 2,
            "metadata": {"owner": "ml", "stage": "gold"},
        },
        None,
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_remote_label_mutation_payload",
            return_value={
                "entity_type": "artifact",
                "artifact_hash": "a" * 64,
                "metadata": {"owner": "ml", "stage": "gold"},
            },
        ) as build_payload,
        patch("roar.application.query.label.GlaasClient", return_value=client),
    ):
        summary = build_push_labels_summary(_push_request(tmp_path))

    build_payload.assert_called_once_with(
        db_ctx,
        roar_dir=tmp_path / ".roar",
        target=resolved_target,
        metadata={"owner": "ml", "stage": "gold"},
    )
    client.patch_current_label.assert_called_once_with(
        {
            "entity_type": "artifact",
            "artifact_hash": "a" * 64,
            "metadata": {"owner": "ml", "stage": "gold"},
        }
    )
    assert summary.heading == "Pushed remote labels (version 2):"
    assert summary.render() == "Pushed remote labels (version 2):\n  owner=ml\n  stage=gold"


def test_build_push_labels_summary_retries_job_push_with_legacy_job_uid_on_not_found(
    tmp_path: Path,
) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    resolved_target = MagicMock(entity_type="job")
    service.resolve_target.return_value = resolved_target
    service.current_metadata.return_value = {"phase": "gold"}
    client = MagicMock()
    client.patch_current_label.side_effect = [
        (None, "HTTP 404: Label not found"),
        ({"version": 3, "metadata": {"phase": "gold"}}, None),
    ]

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
        patch(
            "roar.application.query.label.build_remote_label_mutation_payload",
            side_effect=[
                {
                    "entity_type": "job",
                    "session_hash": "s" * 64,
                    "job_uid": "remote-job-1",
                    "metadata": {"phase": "gold"},
                },
                {
                    "entity_type": "job",
                    "session_hash": "s" * 64,
                    "job_uid": "local-job-1",
                    "metadata": {"phase": "gold"},
                },
            ],
        ) as build_payload,
        patch("roar.application.query.label.GlaasClient", return_value=client),
    ):
        summary = build_push_labels_summary(_push_request(tmp_path, target="@1", entity_type="job"))

    assert build_payload.call_args_list == [
        call(
            db_ctx,
            roar_dir=tmp_path / ".roar",
            target=resolved_target,
            metadata={"phase": "gold"},
        ),
        call(
            db_ctx,
            roar_dir=tmp_path / ".roar",
            target=resolved_target,
            metadata={"phase": "gold"},
            prefer_remote_publication_uid=False,
        ),
    ]
    assert client.patch_current_label.call_args_list == [
        call(
            {
                "entity_type": "job",
                "session_hash": "s" * 64,
                "job_uid": "remote-job-1",
                "metadata": {"phase": "gold"},
            }
        ),
        call(
            {
                "entity_type": "job",
                "session_hash": "s" * 64,
                "job_uid": "local-job-1",
                "metadata": {"phase": "gold"},
            }
        ),
    ]
    assert summary.heading == "Pushed remote labels (version 3):"
    assert summary.render() == "Pushed remote labels (version 3):\n  phase=gold"


def test_build_push_labels_summary_rejects_missing_user_managed_labels(tmp_path: Path) -> None:
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
            build_push_labels_summary(_push_request(tmp_path, target="@1", entity_type="job"))
        except ValueError as exc:
            assert str(exc) == "No local user-managed labels to push for @1."
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
