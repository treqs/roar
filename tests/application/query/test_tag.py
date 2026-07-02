"""Tests for roar.application.query.tag orchestration layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.query.requests import (
    TagAddRequest,
    TagHistoryRequest,
    TagRmRequest,
    TagShowRequest,
)
from roar.application.query.tag import (
    build_tag_add_summary,
    build_tag_history_summary,
    build_tag_rm_summary,
    build_tag_show_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_request(tmp_path: Path, **overrides) -> TagAddRequest:
    return TagAddRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        kv=overrides.pop("kv", "license=MIT"),
        target=overrides.pop("target", "@1"),
        **overrides,
    )


def _rm_request(tmp_path: Path, **overrides) -> TagRmRequest:
    return TagRmRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        key_or_kv=overrides.pop("key_or_kv", "license=MIT"),
        target=overrides.pop("target", "@1"),
        **overrides,
    )


def _show_request(tmp_path: Path, **overrides) -> TagShowRequest:
    return TagShowRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        target=overrides.pop("target", "@1"),
        **overrides,
    )


def _history_request(tmp_path: Path, **overrides) -> TagHistoryRequest:
    return TagHistoryRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        target=overrides.pop("target", "@1"),
        **overrides,
    )


def _mock_db_and_svc(tags: dict, *, changed: bool = True, history: list | None = None):
    """Return (db_ctx_mock, tag_svc_mock) with sensible defaults."""
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None

    svc = MagicMock()
    svc.resolve_target.return_value = object()
    svc.add.return_value = changed
    svc.remove.return_value = changed
    svc.get_tags.return_value = tags
    svc.history.return_value = history or []
    return db_ctx, svc


def _patch(db_ctx, svc):
    return (
        patch("roar.application.query.tag.create_database_context", return_value=db_ctx),
        patch("roar.application.query.tag.TagService", return_value=svc),
    )


# ---------------------------------------------------------------------------
# tag add
# ---------------------------------------------------------------------------

class TestTagAdd:
    def test_reports_tagged_when_value_added(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": ["MIT"]}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        assert "Tagged @1" in summary.heading
        assert "license" in summary.heading

    def test_reports_no_change_when_value_already_present(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": ["MIT"]}, changed=False)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        assert "No change" in summary.heading
        assert "MIT" in summary.heading

    def test_renders_current_tag_entries(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": ["MIT", "Apache-2.0"]})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        keys = [e.key for e in summary.entries]
        assert "license.0" in keys or any("license" in k for k in keys)

    def test_raises_for_missing_equals(self, tmp_path: Path) -> None:
        import pytest
        db_ctx, svc = _mock_db_and_svc({})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1], pytest.raises(ValueError, match="Expected KIND=VALUE"):
            build_tag_add_summary(_add_request(tmp_path, kv="license"))

    def test_raises_for_empty_value(self, tmp_path: Path) -> None:
        import pytest
        db_ctx, svc = _mock_db_and_svc({})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1], pytest.raises(ValueError, match="Value cannot be empty"):
            build_tag_add_summary(_add_request(tmp_path, kv="license="))

    def test_show_empty_message_when_no_tags_after_add(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({}, changed=False)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        assert summary.entries == []


# ---------------------------------------------------------------------------
# tag rm
# ---------------------------------------------------------------------------

class TestTagRm:
    def test_reports_removed_value(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_rm_summary(_rm_request(tmp_path))
        assert "Removed" in summary.heading
        assert "MIT" in summary.heading

    def test_reports_removed_kind_when_no_value(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_rm_summary(_rm_request(tmp_path, key_or_kv="license"))
        assert "Removed" in summary.heading
        assert "tag.license" in summary.heading

    def test_reports_no_change_when_value_absent(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({}, changed=False)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_rm_summary(_rm_request(tmp_path))
        assert "No change" in summary.heading

    def test_renders_remaining_tags(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": ["Apache-2.0"]}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_rm_summary(_rm_request(tmp_path))
        assert any("Apache" in e.display_value for e in summary.entries)


# ---------------------------------------------------------------------------
# tag show
# ---------------------------------------------------------------------------

class TestTagShow:
    def test_renders_current_tags(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": ["MIT"], "contains_pii": ["absent"]})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_show_summary(_show_request(tmp_path))
        keys = [e.key for e in summary.entries]
        assert any("license" in k for k in keys)
        assert any("contains_pii" in k for k in keys)

    def test_renders_no_tags_message_when_empty(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_show_summary(_show_request(tmp_path))
        assert summary.entries == []
        assert "no tags" in summary.render()

    def test_heading_includes_target(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_show_summary(_show_request(tmp_path, target="@2"))
        assert "@2" in summary.heading


# ---------------------------------------------------------------------------
# tag history
# ---------------------------------------------------------------------------

class TestTagHistory:
    def test_renders_version_history(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc(
            {},
            history=[
                {"version": 1, "metadata": {"tag": {"license": ["MIT"]}}},
                {"version": 2, "metadata": {"tag": {"license": ["MIT", "Apache-2.0"]}}},
            ],
        )
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_history_summary(_history_request(tmp_path))
        assert len(summary.versions) == 2
        assert summary.versions[0].version == 1
        assert summary.versions[1].version == 2

    def test_skips_versions_without_tag_namespace(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc(
            {},
            history=[
                {"version": 1, "metadata": {"other": "value"}},
                {"version": 2, "metadata": {"tag": {"license": ["MIT"]}}},
            ],
        )
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_history_summary(_history_request(tmp_path))
        assert len(summary.versions) == 2
        assert summary.versions[0].entries == []

    def test_renders_no_labels_when_empty_history(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({}, history=[])
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_history_summary(_history_request(tmp_path))
        assert summary.render() == "No labels."
