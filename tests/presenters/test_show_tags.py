"""Tests for tag/barrier rendering in `roar show` (and its parity with `roar tag show`).

Tags render in a clean ``Tags:`` section (shared ``tag_display_pairs`` with
`roar tag show`), the internal bind ledger never leaks, and a job's recorded
``--block-tag`` modifiers surface as ``Barriers:``.
"""

from __future__ import annotations

from roar.application.tags import barrier_items, tag_display_pairs
from roar.presenters.show_renderer import ShowRenderer

_TAG_LABELS = {
    "tag": {
        "contains_pii": {"values": [{"value": "present", "origin": "user"}]},
        "license": {
            "values": [
                {"value": "MIT", "origin": "user"},
                {"value": "GPL-3.0", "origin": "system", "job": "j"},
            ]
        },
        "bind": {"events": [{"action": "bind", "covers": {"contains_pii": ["present"]}}]},
    },
    "other_label": "keep-me",
}


class TestTagDisplayPairs:
    def test_sorted_skips_bind_and_joins_values(self) -> None:
        assert tag_display_pairs(_TAG_LABELS["tag"]) == [
            ("contains_pii", "present"),
            ("license", "MIT, GPL-3.0"),
        ]

    def test_non_dict_is_empty(self) -> None:
        assert tag_display_pairs(None) == []
        assert tag_display_pairs("nope") == []


class TestBarrierItems:
    def test_from_run_modifiers(self) -> None:
        assert barrier_items({"block_tags": ["contains_pii", "license=GPL-3.0"]}) == [
            "contains_pii",
            "license=GPL-3.0",
        ]

    def test_non_dict_or_empty(self) -> None:
        assert barrier_items(None) == []
        assert barrier_items({}) == []
        assert barrier_items({"add_tags": ["x=y"]}) == []


class TestRenderTags:
    def test_clean_section_no_internals(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_tags(lines, _TAG_LABELS)
        out = "\n".join(lines)
        assert "Tags:" in out
        assert "contains_pii=present" in out
        assert "license=MIT, GPL-3.0" in out
        # neither the bind ledger nor the raw value-record structure leaks
        assert "bind" not in out
        assert "values" not in out
        assert "origin" not in out

    def test_no_tags_no_section(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_tags(lines, {"other_label": "x"})
        assert lines == []


class TestRenderLabelsExcludesTags:
    def test_tag_subtree_kept_out_of_raw_labels(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_labels(lines, _TAG_LABELS)
        out = "\n".join(lines)
        assert "Labels:" in out
        assert "other_label=keep-me" in out
        assert "tag." not in out  # incl. the bind ledger — no raw tag.* dump

    def test_tags_only_yields_no_labels_section(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_labels(lines, {"tag": _TAG_LABELS["tag"]})
        assert lines == []


class TestRenderBarriers:
    def test_barriers_section(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_barriers(lines, {"run_modifiers": {"block_tags": ["contains_pii"]}})
        out = "\n".join(lines)
        assert "Barriers:" in out
        assert "contains_pii" in out
        assert "--block-tag" in out

    def test_no_run_modifiers_no_section(self) -> None:
        lines: list[str] = []
        ShowRenderer._render_barriers(lines, {"runtime": {}})
        assert lines == []
