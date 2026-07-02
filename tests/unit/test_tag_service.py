"""Unit tests for TagService set-accumulation semantics.

TagService wraps LabelService with array set semantics over the tag.* namespace.
These tests mock LabelService to focus on TagService's own logic without
requiring the full project dependency chain (blake3, SQLAlchemy, etc.).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from roar.application.labels import LabelTargetRef, LabelWriteResult
from roar.application.tags import TagService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLVED = LabelTargetRef(entity_type="job", job_id=1, display_target="@1")


def _make_service(current_metadata: dict | None = None):
    """Return (TagService, mock_label_service) with stubbed current_metadata."""
    db_ctx = MagicMock()
    svc = TagService(db_ctx, Path("."))
    mock_label_svc = MagicMock()
    mock_label_svc.current_metadata.return_value = current_metadata or {}
    mock_label_svc.set_metadata.return_value = LabelWriteResult(
        changed=True, metadata={}, version=1
    )
    mock_label_svc.delete_keys.return_value = LabelWriteResult(
        changed=True, metadata={}, version=1
    )
    svc._svc = mock_label_svc
    return svc, mock_label_svc


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_first_value_creates_list(self) -> None:
        svc, label_svc = _make_service()
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is True
        label_svc.set_metadata.assert_called_once_with(_RESOLVED, {"tag": {"license": ["MIT"]}})

    def test_add_second_value_appends_to_existing_list(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        changed = svc.add(_RESOLVED, "license", "Apache-2.0")
        assert changed is True
        label_svc.set_metadata.assert_called_once_with(
            _RESOLVED, {"tag": {"license": ["MIT", "Apache-2.0"]}}
        )

    def test_add_duplicate_returns_false_without_writing(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is False
        label_svc.set_metadata.assert_not_called()
        label_svc.delete_keys.assert_not_called()

    def test_add_promotes_legacy_scalar_to_list(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": "MIT"}})
        changed = svc.add(_RESOLVED, "license", "Apache-2.0")
        assert changed is True
        label_svc.set_metadata.assert_called_once_with(
            _RESOLVED, {"tag": {"license": ["MIT", "Apache-2.0"]}}
        )

    def test_add_duplicate_scalar_returns_false(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": "MIT"}})
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is False
        label_svc.set_metadata.assert_not_called()

    def test_add_different_kind_does_not_touch_others(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        svc.add(_RESOLVED, "contains_pii", "absent")
        label_svc.set_metadata.assert_called_once_with(
            _RESOLVED, {"tag": {"contains_pii": ["absent"]}}
        )

    def test_add_when_no_tag_namespace_yet(self) -> None:
        svc, label_svc = _make_service({"owner": "ml"})
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is True
        label_svc.set_metadata.assert_called_once_with(_RESOLVED, {"tag": {"license": ["MIT"]}})


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_specific_value(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT", "Apache-2.0"]}})
        changed = svc.remove(_RESOLVED, "license", "MIT")
        assert changed is True
        label_svc.set_metadata.assert_called_once_with(
            _RESOLVED, {"tag": {"license": ["Apache-2.0"]}}
        )

    def test_remove_last_value_calls_delete_keys(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        changed = svc.remove(_RESOLVED, "license", "MIT")
        assert changed is True
        label_svc.delete_keys.assert_called_once_with(_RESOLVED, ["tag.license"])
        label_svc.set_metadata.assert_not_called()

    def test_remove_whole_kind_calls_delete_keys(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT", "Apache-2.0"]}})
        changed = svc.remove(_RESOLVED, "license", None)
        assert changed is True
        label_svc.delete_keys.assert_called_once_with(_RESOLVED, ["tag.license"])

    def test_remove_absent_kind_returns_false(self) -> None:
        svc, label_svc = _make_service({})
        changed = svc.remove(_RESOLVED, "license", None)
        assert changed is False
        label_svc.set_metadata.assert_not_called()
        label_svc.delete_keys.assert_not_called()

    def test_remove_absent_value_returns_false(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        changed = svc.remove(_RESOLVED, "license", "GPL-3.0")
        assert changed is False
        label_svc.set_metadata.assert_not_called()
        label_svc.delete_keys.assert_not_called()

    def test_remove_scalar_value_when_matches(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": "MIT"}})
        changed = svc.remove(_RESOLVED, "license", "MIT")
        assert changed is True
        label_svc.delete_keys.assert_called_once_with(_RESOLVED, ["tag.license"])

    def test_remove_reflects_delete_keys_changed_result(self) -> None:
        svc, label_svc = _make_service({"tag": {"license": ["MIT"]}})
        label_svc.delete_keys.return_value = LabelWriteResult(
            changed=False, metadata={}, version=1
        )
        changed = svc.remove(_RESOLVED, "license", None)
        assert changed is False


# ---------------------------------------------------------------------------
# get_tags
# ---------------------------------------------------------------------------

class TestGetTags:
    def test_returns_tag_namespace_subtree(self) -> None:
        svc, _ = _make_service({"tag": {"license": ["MIT"]}, "owner": "ml"})
        assert svc.get_tags(_RESOLVED) == {"license": ["MIT"]}

    def test_returns_empty_dict_when_no_tags(self) -> None:
        svc, _ = _make_service({"owner": "ml"})
        assert svc.get_tags(_RESOLVED) == {}

    def test_returns_empty_dict_when_no_metadata(self) -> None:
        svc, _ = _make_service()
        assert svc.get_tags(_RESOLVED) == {}


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

class TestHistory:
    def test_delegates_to_label_service(self) -> None:
        svc, label_svc = _make_service()
        expected = [{"version": 1, "metadata": {}}]
        label_svc.history.return_value = expected
        result = svc.history(_RESOLVED)
        label_svc.history.assert_called_once_with(_RESOLVED)
        assert result is expected


# ---------------------------------------------------------------------------
# resolve_target — error cases only (happy path needs DB repos)
# ---------------------------------------------------------------------------

class TestResolveTargetErrors:
    def test_rejects_build_step_reference(self) -> None:
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        with pytest.raises(ValueError, match="Build-step targets"):
            svc.resolve_target("@B1")

    def test_rejects_session_reference(self) -> None:
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        with pytest.raises(ValueError, match="Session targets"):
            svc.resolve_target("@session")

    def test_rejects_latest_reference(self) -> None:
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        with pytest.raises(ValueError, match="Session targets"):
            svc.resolve_target("@latest")

    def test_at_n_delegates_to_label_service_as_job(self) -> None:
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        svc._svc = MagicMock()
        svc._svc.resolve_target.return_value = _RESOLVED
        result = svc.resolve_target("@1")
        svc._svc.resolve_target.assert_called_once_with("job", "@1")
        assert result is _RESOLVED

    def test_hex_string_delegates_to_label_service_as_artifact(self) -> None:
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        resolved = LabelTargetRef(entity_type="artifact", artifact_id="abc", display_target="abc")
        svc._svc = MagicMock()
        svc._svc.resolve_target.return_value = resolved
        result = svc.resolve_target("a1b2c3d4")
        svc._svc.resolve_target.assert_called_once_with("artifact", "a1b2c3d4")
        assert result is resolved
