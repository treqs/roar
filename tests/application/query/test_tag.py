"""Tests for roar.application.query.tag orchestration layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.query.requests import (
    TagAddRequest,
    TagBindRequest,
    TagHistoryRequest,
    TagRmRequest,
    TagShowRequest,
    TagUnbindRequest,
)
from roar.application.query.tag import (
    build_tag_add_summary,
    build_tag_bind_summary,
    build_tag_history_summary,
    build_tag_rm_summary,
    build_tag_show_summary,
    build_tag_unbind_summary,
)
from roar.application.tags import BindResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kind(*values: str, origin: str = "user") -> dict:
    return {"values": [{"value": v, "origin": origin} for v in values]}


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


def _bind_request(tmp_path: Path, **overrides) -> TagBindRequest:
    return TagBindRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        targets=overrides.pop("targets", ("model.pt",)),
        **overrides,
    )


def _unbind_request(tmp_path: Path, **overrides) -> TagUnbindRequest:
    return TagUnbindRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        targets=overrides.pop("targets", ("model.pt",)),
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
        db_ctx, svc = _mock_db_and_svc({"license": _kind("MIT")}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        assert "Tagged @1" in summary.heading
        assert "license" in summary.heading

    def test_reports_no_change_when_value_already_present(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": _kind("MIT")}, changed=False)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        assert "No change" in summary.heading
        assert "MIT" in summary.heading

    def test_renders_current_tag_entries(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": _kind("MIT", "Apache-2.0")})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_add_summary(_add_request(tmp_path))
        entry = next(e for e in summary.entries if e.key == "license")
        assert entry.display_value == "MIT, Apache-2.0"

    def test_raises_for_missing_equals(self, tmp_path: Path) -> None:
        import pytest

        db_ctx, svc = _mock_db_and_svc({})
        with (
            _patch(db_ctx, svc)[0],
            _patch(db_ctx, svc)[1],
            pytest.raises(ValueError, match="Expected KIND=VALUE"),
        ):
            build_tag_add_summary(_add_request(tmp_path, kv="license"))

    def test_raises_for_empty_value(self, tmp_path: Path) -> None:
        import pytest

        db_ctx, svc = _mock_db_and_svc({})
        with (
            _patch(db_ctx, svc)[0],
            _patch(db_ctx, svc)[1],
            pytest.raises(ValueError, match="Value cannot be empty"),
        ):
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
        db_ctx, svc = _mock_db_and_svc({"license": _kind("Apache-2.0")}, changed=True)
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_rm_summary(_rm_request(tmp_path))
        assert any("Apache" in e.display_value for e in summary.entries)


# ---------------------------------------------------------------------------
# tag show
# ---------------------------------------------------------------------------


class TestTagShow:
    def test_renders_current_tags(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc({"license": _kind("MIT"), "contains_pii": _kind("absent")})
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_show_summary(_show_request(tmp_path))
        keys = [e.key for e in summary.entries]
        assert "license" in keys
        assert "contains_pii" in keys

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
                {"version": 1, "metadata": {"tag": {"license": _kind("MIT")}}},
                {"version": 2, "metadata": {"tag": {"license": _kind("MIT", "Apache-2.0")}}},
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
                {"version": 2, "metadata": {"tag": {"license": _kind("MIT")}}},
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

    def test_bind_ledger_is_excluded_from_history_entries(self, tmp_path: Path) -> None:
        db_ctx, svc = _mock_db_and_svc(
            {},
            history=[
                {
                    "version": 1,
                    "metadata": {
                        "tag": {
                            "license": _kind("MIT"),
                            "bind": {
                                "events": [{"action": "bind", "covers": {"license": ["MIT"]}}]
                            },
                        }
                    },
                },
            ],
        )
        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_history_summary(_history_request(tmp_path))
        keys = [e.key for e in summary.versions[0].entries]
        assert keys == ["license"]


# ---------------------------------------------------------------------------
# tag bind / unbind
# ---------------------------------------------------------------------------


class TestTagBind:
    def test_binds_each_target_and_echoes_promoted_tags(self, tmp_path: Path) -> None:
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db_ctx
        db_ctx.__exit__.return_value = None
        db_ctx.artifacts.get.return_value = {"size": 1024}

        svc = MagicMock()
        resolved = MagicMock(entity_type="artifact", artifact_id="a1", display_target="model.pt")
        svc.resolve_target.return_value = resolved
        svc.bind.return_value = BindResult(changed=True, promoted={"license": ["MIT"]})

        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_bind_summary(_bind_request(tmp_path, targets=("model.pt",)))

        assert len(summary.artifacts) == 1
        entry = summary.artifacts[0]
        assert entry.display_target == "model.pt"
        assert entry.action == "bind"
        assert entry.changed is True
        assert entry.promoted == {"license": ["MIT"]}
        assert entry.size == 1024

    def test_binds_multiple_targets_in_order(self, tmp_path: Path) -> None:
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db_ctx
        db_ctx.__exit__.return_value = None
        db_ctx.artifacts.get.return_value = None

        svc = MagicMock()
        svc.resolve_target.side_effect = [
            MagicMock(entity_type="artifact", artifact_id="a1", display_target="one.pt"),
            MagicMock(entity_type="artifact", artifact_id="a2", display_target="two.pt"),
        ]
        svc.bind.return_value = BindResult(changed=False, promoted={})

        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_bind_summary(_bind_request(tmp_path, targets=("one.pt", "two.pt")))

        assert [a.display_target for a in summary.artifacts] == ["one.pt", "two.pt"]

    def test_no_change_renders_a_no_op_line(self, tmp_path: Path) -> None:
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db_ctx
        db_ctx.__exit__.return_value = None
        db_ctx.artifacts.get.return_value = {"size": 0}

        svc = MagicMock()
        svc.resolve_target.return_value = MagicMock(
            entity_type="artifact", artifact_id="a1", display_target="empty.bin"
        )
        svc.bind.return_value = BindResult(changed=False, promoted={})

        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_bind_summary(_bind_request(tmp_path))

        rendered = summary.render()
        assert "no change" in rendered
        assert "empty-content hash" in rendered  # size == 0 warning


class TestTagUnbind:
    def test_unbinds_each_target(self, tmp_path: Path) -> None:
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db_ctx
        db_ctx.__exit__.return_value = None
        db_ctx.artifacts.get.return_value = {"size": 2048}

        svc = MagicMock()
        svc.resolve_target.return_value = MagicMock(
            entity_type="artifact", artifact_id="a1", display_target="model.pt"
        )
        svc.unbind.return_value = BindResult(changed=True, promoted={"license": ["MIT"]})

        with _patch(db_ctx, svc)[0], _patch(db_ctx, svc)[1]:
            summary = build_tag_unbind_summary(_unbind_request(tmp_path))

        assert summary.artifacts[0].action == "unbind"
        assert "Unbound" in summary.render()
        svc.unbind.assert_called_once()
        svc.bind.assert_not_called()
