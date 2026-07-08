"""Unit tests for TagService set-accumulation + bind-ledger semantics.

TagService writes directly through the raw label repository (db_ctx.labels),
not LabelService — its own reserved-namespace writes must bypass the
tag.*/attach.* reservation that protects the generic `roar label` path (see
system_labels.py). These tests mock that repository directly rather than
requiring the full project dependency chain (blake3, SQLAlchemy, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from roar.application.labels import LabelTargetRef
from roar.application.tags import TagService
from roar.core.label_origins import LABEL_ORIGIN_USER

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLVED = LabelTargetRef(entity_type="job", job_id=1, display_target="@1")


def _make_service(current_metadata: dict[str, Any] | None = None):
    """Return (TagService, mock_label_repo) with stubbed get_current/create_version."""
    db_ctx = MagicMock()
    mock_label_repo = db_ctx.labels
    mock_label_repo.get_current.return_value = (
        {"metadata": current_metadata, "version": 1} if current_metadata is not None else None
    )
    svc = TagService(db_ctx, Path("."))
    return svc, mock_label_repo


def _written_tag_subtree(mock_label_repo: MagicMock) -> dict[str, Any]:
    """The `tag` subtree from the most recent create_version call's metadata."""
    metadata = mock_label_repo.create_version.call_args.args[1]
    return metadata.get("tag", {})


def _values(kind_data: dict[str, Any]) -> list[str]:
    return [record["value"] for record in kind_data["values"]]


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_add_first_value_writes_user_origin_and_implicit_bind(self) -> None:
        svc, label_repo = _make_service()
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is True
        tag = _written_tag_subtree(label_repo)
        assert tag["license"] == {"values": [{"value": "MIT", "origin": LABEL_ORIGIN_USER}]}
        assert tag["bind"] == {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]}

    def test_add_second_value_appends_to_existing_list(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        changed = svc.add(_RESOLVED, "license", "Apache-2.0")
        assert changed is True
        tag = _written_tag_subtree(label_repo)
        assert _values(tag["license"]) == ["MIT", "Apache-2.0"]
        # Implicit bind covers only the value just added, not the pre-existing one.
        assert tag["bind"]["events"][-1] == {
            "action": "bind",
            "covers": {"license": ["Apache-2.0"]},
        }

    def test_add_duplicate_returns_false_without_writing(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is False
        label_repo.create_version.assert_not_called()

    def test_add_different_kind_does_not_touch_others(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        svc.add(_RESOLVED, "contains_pii", "absent")
        tag = _written_tag_subtree(label_repo)
        assert _values(tag["license"]) == ["MIT"]
        assert _values(tag["contains_pii"]) == ["absent"]

    def test_add_when_no_tag_namespace_yet_preserves_other_metadata(self) -> None:
        svc, label_repo = _make_service({"owner": "ml"})
        changed = svc.add(_RESOLVED, "license", "MIT")
        assert changed is True
        metadata = label_repo.create_version.call_args.args[1]
        assert metadata["owner"] == "ml"
        assert _values(metadata["tag"]["license"]) == ["MIT"]

    def test_add_appends_to_an_existing_bind_ledger_rather_than_overwriting(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "bind": {"events": [{"action": "bind", "covers": {"jurisdiction": ["EU"]}}]},
                }
            }
        )
        svc.add(_RESOLVED, "license", "MIT")
        tag = _written_tag_subtree(label_repo)
        assert tag["bind"]["events"] == [
            {"action": "bind", "covers": {"jurisdiction": ["EU"]}},
            {"action": "bind", "covers": {"license": ["MIT"]}},
        ]


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_specific_value(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {
                        "values": [
                            {"value": "MIT", "origin": "user"},
                            {"value": "Apache-2.0", "origin": "user"},
                        ]
                    }
                }
            }
        )
        changed = svc.remove(_RESOLVED, "license", "MIT")
        assert changed is True
        tag = _written_tag_subtree(label_repo)
        assert _values(tag["license"]) == ["Apache-2.0"]

    def test_remove_last_value_drops_the_kind(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        changed = svc.remove(_RESOLVED, "license", "MIT")
        assert changed is True
        tag = _written_tag_subtree(label_repo)
        assert "license" not in tag

    def test_remove_whole_kind(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {
                        "values": [
                            {"value": "MIT", "origin": "user"},
                            {"value": "Apache-2.0", "origin": "user"},
                        ]
                    }
                }
            }
        )
        changed = svc.remove(_RESOLVED, "license", None)
        assert changed is True
        tag = _written_tag_subtree(label_repo)
        assert "license" not in tag

    def test_remove_absent_kind_returns_false(self) -> None:
        svc, label_repo = _make_service({})
        changed = svc.remove(_RESOLVED, "license", None)
        assert changed is False
        label_repo.create_version.assert_not_called()

    def test_remove_absent_value_returns_false(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        changed = svc.remove(_RESOLVED, "license", "GPL-3.0")
        assert changed is False
        label_repo.create_version.assert_not_called()

    def test_remove_does_not_touch_the_bind_ledger(self) -> None:
        """rm is a history event, not a hard delete — past binds stay, append-only."""
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                }
            }
        )
        svc.remove(_RESOLVED, "license", "MIT")
        tag = _written_tag_subtree(label_repo)
        assert tag["bind"] == {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]}


# ---------------------------------------------------------------------------
# bind / unbind
# ---------------------------------------------------------------------------


class TestBind:
    def test_bind_covers_every_current_kind_and_value(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "jurisdiction": {"values": [{"value": "EU", "origin": "system", "job": "j1"}]},
                }
            }
        )
        result = svc.bind(_RESOLVED)
        assert result.changed is True
        assert result.promoted == {"license": ["MIT"], "jurisdiction": ["EU"]}
        tag = _written_tag_subtree(label_repo)
        assert tag["bind"]["events"][-1] == {
            "action": "bind",
            "covers": {"license": ["MIT"], "jurisdiction": ["EU"]},
        }

    def test_bind_appends_to_an_existing_ledger_when_the_covered_set_grew(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "jurisdiction": {"values": [{"value": "EU", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                }
            }
        )
        result = svc.bind(_RESOLVED)
        assert result.changed is True
        tag = _written_tag_subtree(label_repo)
        assert len(tag["bind"]["events"]) == 2

    def test_rebinding_the_exact_same_set_is_a_noop(self) -> None:
        """Re-binding an artifact whose tags haven't changed since the last bind
        shouldn't add a redundant ledger entry every time `roar register` runs."""
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                }
            }
        )
        result = svc.bind(_RESOLVED)
        assert result.changed is False
        assert result.promoted == {"license": ["MIT"]}
        label_repo.create_version.assert_not_called()

    def test_bind_with_no_tags_is_a_noop(self) -> None:
        svc, label_repo = _make_service({})
        result = svc.bind(_RESOLVED)
        assert result.changed is False
        assert result.promoted == {}
        label_repo.create_version.assert_not_called()


class TestUnbind:
    def test_unbind_revokes_currently_bound_pairs(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                }
            }
        )
        result = svc.unbind(_RESOLVED)
        assert result.changed is True
        assert result.promoted == {"license": ["MIT"]}
        tag = _written_tag_subtree(label_repo)
        assert tag["bind"]["events"][-1] == {"action": "unbind", "covers": {"license": ["MIT"]}}

    def test_unbind_with_nothing_ever_bound_is_a_noop(self) -> None:
        svc, label_repo = _make_service(
            {"tag": {"license": {"values": [{"value": "MIT", "origin": "user"}]}}}
        )
        result = svc.unbind(_RESOLVED)
        assert result.changed is False
        label_repo.create_version.assert_not_called()

    def test_unbind_after_unbind_is_a_noop(self) -> None:
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {
                        "events": [
                            {"action": "bind", "covers": {"license": ["MIT"]}},
                            {"action": "unbind", "covers": {"license": ["MIT"]}},
                        ]
                    },
                }
            }
        )
        result = svc.unbind(_RESOLVED)
        assert result.changed is False
        label_repo.create_version.assert_not_called()

    def test_unbind_does_not_delete_the_revoked_event(self) -> None:
        """Append-only revocation: unbind writes a new event, never deletes the bind."""
        svc, label_repo = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                }
            }
        )
        svc.unbind(_RESOLVED)
        tag = _written_tag_subtree(label_repo)
        assert tag["bind"]["events"][0] == {"action": "bind", "covers": {"license": ["MIT"]}}
        assert len(tag["bind"]["events"]) == 2


# ---------------------------------------------------------------------------
# get_tags
# ---------------------------------------------------------------------------


class TestGetTags:
    def test_returns_tag_namespace_subtree_excluding_the_bind_ledger(self) -> None:
        svc, _ = _make_service(
            {
                "tag": {
                    "license": {"values": [{"value": "MIT", "origin": "user"}]},
                    "bind": {"events": [{"action": "bind", "covers": {"license": ["MIT"]}}]},
                },
                "owner": "ml",
            }
        )
        assert svc.get_tags(_RESOLVED) == {
            "license": {"values": [{"value": "MIT", "origin": "user"}]}
        }

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
        db_ctx = MagicMock()
        svc = TagService(db_ctx, Path("."))
        mock_label_svc = MagicMock()
        expected = [{"version": 1, "metadata": {}}]
        mock_label_svc.history.return_value = expected
        svc._svc = mock_label_svc
        result = svc.history(_RESOLVED)
        mock_label_svc.history.assert_called_once_with(_RESOLVED)
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
