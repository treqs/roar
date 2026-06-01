"""Unit tests for ShowRenderer._render_composite_provenance — surfaces a
composite's HF source and subset_of relation from artifact metadata."""

from __future__ import annotations

import json

from roar.presenters.show_renderer import ShowRenderer


def _render(metadata) -> list[str]:
    lines: list[str] = []
    ShowRenderer._render_composite_provenance(lines, {"metadata": metadata})
    return lines


def test_no_metadata_renders_nothing():
    assert _render(None) == []
    assert _render("") == []
    assert _render("not json{") == []
    assert _render("[1, 2]") == []  # valid JSON but not a dict


def test_hf_source_rendered_with_commit():
    meta = {
        "hf": {"repo_type": "datasets", "repo": "openai/gsm8k", "commit": "740312add88f7819abc"}
    }
    lines = _render(json.dumps(meta))
    assert lines == ["Source:     hf://datasets/openai/gsm8k@740312add88f"]


def test_hf_source_without_commit():
    lines = _render({"hf": {"repo": "ylecun/mnist"}})
    assert lines == ["Source:     hf://datasets/ylecun/mnist"]


def test_subset_of_rendered_with_selector():
    meta = {"subset_of": {"dataset": "hf://datasets/openai/gsm8k@abc", "selector": "first:3"}}
    assert _render(meta) == ["Subset of:  hf://datasets/openai/gsm8k@abc  (first:3)"]


def test_subset_of_without_selector():
    assert _render({"subset_of": {"dataset": "hf://datasets/x/y"}}) == [
        "Subset of:  hf://datasets/x/y"
    ]


def test_source_and_subset_together():
    meta = {
        "hf": {"repo": "openai/gsm8k", "commit": "abcdef012345"},
        "subset_of": {"dataset": "hf://datasets/openai/gsm8k@abcdef012345", "selector": "first:3"},
    }
    lines = _render(json.dumps(meta))
    assert lines == [
        "Source:     hf://datasets/openai/gsm8k@abcdef012345",
        "Subset of:  hf://datasets/openai/gsm8k@abcdef012345  (first:3)",
    ]
