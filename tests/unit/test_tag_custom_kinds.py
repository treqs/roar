"""Unit tests for tag-kind enforcement (roar/cli/_tag_kinds).

Non-canonical kinds are rejected unless allowed via `[tags] custom_kinds` in
.roarconfig; the rejection prints a spelled-out, append-aware hint. config_get
is patched so these tests don't depend on a real config file on disk.
"""

from __future__ import annotations

import pytest

from roar.cli import _tag_kinds
from roar.core.label_constants import CANONICAL_TAG_KINDS


def _patch_config(monkeypatch, custom_kinds):
    def fake_config_get(key, start_dir=None):
        if key == "tags.custom_kinds":
            return custom_kinds
        if key == "hints.enabled":
            return True
        if key == "output.verbosity":
            return "normal"
        return None

    monkeypatch.setattr("roar.integrations.config.config_get", fake_config_get)


class TestResolve:
    def test_canonical_kinds_always_allowed(self, monkeypatch):
        _patch_config(monkeypatch, [])
        assert _tag_kinds.allowed_tag_kinds() >= CANONICAL_TAG_KINDS

    def test_configured_custom_kinds_are_allowed(self, monkeypatch):
        _patch_config(monkeypatch, ["export_control", "data_retention"])
        allowed = _tag_kinds.allowed_tag_kinds()
        assert {"export_control", "data_retention"} <= allowed

    def test_custom_kinds_are_deduped_and_stripped(self, monkeypatch):
        _patch_config(monkeypatch, [" export_control ", "export_control", "", "  "])
        assert _tag_kinds.configured_custom_kinds() == ["export_control"]

    def test_non_list_config_is_ignored(self, monkeypatch):
        _patch_config(monkeypatch, None)
        assert _tag_kinds.configured_custom_kinds() == []


class TestEnforce:
    def test_canonical_passes_silently(self, monkeypatch):
        _patch_config(monkeypatch, [])
        _tag_kinds.enforce_tag_kind("license")  # no raise

    def test_configured_custom_passes_silently(self, monkeypatch):
        _patch_config(monkeypatch, ["export_control"])
        _tag_kinds.enforce_tag_kind("export_control")  # no raise

    def test_unknown_kind_exits_nonzero_with_spelled_out_hint(self, monkeypatch, capsys):
        _patch_config(monkeypatch, [])
        with pytest.raises(SystemExit) as exc_info:
            _tag_kinds.enforce_tag_kind("risk_tier")
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "'risk_tier' is not a canonical tag kind" in err
        assert "[tags]" in err
        assert 'custom_kinds = ["risk_tier"]' in err

    def test_hints_suppressed_still_errors(self, monkeypatch, capsys):
        def fake(key, start_dir=None):
            if key == "output.verbosity":
                return "quiet"  # suppresses hints
            if key == "tags.custom_kinds":
                return []
            return None

        monkeypatch.setattr("roar.integrations.config.config_get", fake)
        with pytest.raises(SystemExit):
            _tag_kinds.enforce_tag_kind("risk_tier")
        err = capsys.readouterr().err
        assert "not a canonical tag kind" in err
        assert "hint:" not in err  # hints gated off, but the error still shows


class TestHint:
    def test_add_verb_and_snippet_when_no_existing(self, monkeypatch):
        _patch_config(monkeypatch, [])
        lines = _tag_kinds._hint_lines("risk_tier", None)
        assert lines[0].startswith("to allow 'risk_tier', add ")
        assert lines[1] == "    [tags]"
        assert lines[2] == '    custom_kinds = ["risk_tier"]'

    def test_appends_to_existing_custom_kinds(self, monkeypatch):
        _patch_config(monkeypatch, ["data_retention"])
        lines = _tag_kinds._hint_lines("export_control", None)
        assert "update" in lines[0]
        assert lines[2] == '    custom_kinds = ["data_retention", "export_control"]'
