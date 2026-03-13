from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.query import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
)
from roar.application.query.label import (
    build_copy_labels_summary,
    build_label_history_summary,
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


def test_build_set_labels_summary_returns_typed_summary(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    service = MagicMock()
    service.resolve_target.return_value = object()
    service.set_metadata.return_value = MagicMock(
        changed=True,
        version=2,
        metadata={"owner": "ml", "stage": "gold"},
    )

    with (
        patch("roar.application.query.label.create_database_context", return_value=db_ctx),
        patch("roar.application.query.label.LabelService", return_value=service),
    ):
        summary = build_set_labels_summary(_set_request(tmp_path))

    assert summary.heading == "Updated labels (version 2):"
    assert [(entry.key, entry.display_value) for entry in summary.entries] == [
        ("owner", "ml"),
        ("stage", "gold"),
    ]
    assert summary.render() == "Updated labels (version 2):\n  owner=ml\n  stage=gold"


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
