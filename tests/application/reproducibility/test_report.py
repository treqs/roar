"""Tests for the shared reproducibility checklist."""

from roar.application.reproducibility.report import build_report, render_report


def _full_report(**overrides):
    facts = {
        "committed": True,
        "pushed": True,
        "runtime_ok": True,
        "unsourced_paths": [],
        "on_glaas": True,
    }
    facts.update(overrides)
    return build_report(**facts)


def test_all_green_collapses_to_one_line():
    out = render_report(_full_report())
    assert out == "Reproducibility: ✓ all 5 checks passed"


def test_failed_boxes_expand_passed_collapse():
    report = _full_report(committed=False, on_glaas=False)
    out = render_report(report)
    assert "3/5 checks passed" in out
    # failed boxes are expanded with detail...
    assert "[ ] code committed to git" in out
    assert "isn't versioned" in out
    assert "[ ] lineage saved on glaas.ai" in out
    # ...passed ones are folded into a single [x] line
    assert "[x]" in out
    assert "commit reachable on a remote" in out  # appears in the folded passed line
    assert "may not reproduce as recorded" in out


def test_unsourced_detail_lists_paths_and_flags_tmp():
    report = _full_report(unsourced_paths=["/data/events.csv", "/tmp/scratch.parquet"])
    out = render_report(report)
    assert "[ ] all inputs sourced" in out
    assert "2 input(s)" in out
    assert "/data/events.csv" in out
    assert "1 in /tmp" in out


def test_links_appended():
    out = render_report(
        _full_report(),
        links=[("Session", "https://glaas.ai/dag/abc"), ("Artifact", "https://glaas.ai/a/def")],
    )
    assert "Session: https://glaas.ai/dag/abc" in out
    assert "Artifact: https://glaas.ai/a/def" in out


def test_check_keys_are_stable_and_complete():
    keys = [c.key for c in _full_report().checks]
    assert keys == ["committed", "pushed", "inputs_sourced", "runtime", "on_glaas"]
