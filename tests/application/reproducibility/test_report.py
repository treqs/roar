"""Tests for the shared reproducibility checklist."""

from roar.application.reproducibility.report import (
    build_report,
    is_shareable_remote,
    render_report,
)


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


def test_all_green_shows_every_check():
    out = render_report(_full_report())
    assert "Reproducibility — 6/6" in out
    # every check is listed (no collapsing)
    assert out.count("[✅]") == 6
    assert "[❌]" not in out
    assert "code committed to git" in out
    assert "single git commit across all steps" in out
    assert "lineage saved on glaas.ai" in out
    # all green -> no warning
    assert "may not reproduce" not in out


def test_failed_checks_expand_with_exception():
    out = render_report(_full_report(committed=False, on_glaas=False))
    assert "Reproducibility — 4/6" in out
    assert out.count("[❌]") == 2
    assert out.count("[✅]") == 4
    assert "[❌] code committed to git" in out
    assert "→ run outside a git repo" in out  # the exception detail
    assert "may not reproduce as recorded" in out


def test_multi_commit_fails_single_commit_check():
    out = render_report(_full_report(single_commit=False))
    assert "[❌] single git commit across all steps" in out
    assert "→ steps span more than one commit" in out
    assert "may not reproduce as recorded" in out


def test_unsourced_detail_lists_paths_and_flags_tmp():
    out = render_report(_full_report(unsourced_paths=["/data/events.csv", "/tmp/scratch.parquet"]))
    assert "[❌] all inputs sourced" in out
    assert "2 input(s)" in out
    assert "/data/events.csv" in out
    assert "1 in /tmp" in out


def test_na_marker_renders_dash_and_is_excluded_from_count():
    # dry-run marks publish status n/a — neither pass nor fail.
    report = build_report(
        committed=True,
        pushed=True,
        runtime_ok=True,
        unsourced_paths=[],
        on_glaas=False,
        na={"on_glaas": "dry run — nothing published yet"},
    )
    out = render_report(report)
    assert "[-] lineage saved on glaas.ai" in out
    assert "dry run — nothing published yet" in out
    # n/a excluded from the denominator: 5 applicable checks, all green
    assert "Reproducibility — 5/5" in out
    # an n/a item is not a failure, so no warning
    assert "may not reproduce" not in out


def test_check_keys_are_stable_and_complete():
    keys = [c.key for c in _full_report().checks]
    assert keys == ["committed", "single_commit", "pushed", "inputs_sourced", "runtime", "on_glaas"]


def test_is_shareable_remote():
    assert is_shareable_remote("https://github.com/u/r.git")
    assert is_shareable_remote("git@github.com:u/r.git")
    assert is_shareable_remote("ssh://git@host/u/r")
    assert is_shareable_remote("git://host/r")
    # local / not shareable — the P0 fix: file:// must not read as a remote
    assert not is_shareable_remote("file:///home/ubuntu/proj")
    assert not is_shareable_remote("/home/ubuntu/proj")
    assert not is_shareable_remote("~/proj")
    assert not is_shareable_remote("")
    assert not is_shareable_remote(None)
