"""Tests for the shared reproducibility checklist."""

import subprocess
from pathlib import Path

from roar.application.reproducibility.report import (
    _dir_will_exist_on_checkout,
    _resolve_repo_root,
    build_report,
    is_shareable_remote,
    render_report,
    untracked_artifact_dirs,
)


def _full_report(**overrides):
    # A register/put-style report: every fact supplied, so all checks render
    # (including the receipt-only `paths_tracked` and `on_glaas`).
    facts = {
        "committed": True,
        "pushed": True,
        "runtime_ok": True,
        "unsourced_paths": [],
        "untracked_paths": [],
        "on_glaas": True,
    }
    facts.update(overrides)
    return build_report(**facts)


def test_all_green_shows_every_check():
    out = render_report(_full_report())
    assert "Reproducibility — 7/7" in out
    # every check is listed (no collapsing)
    assert out.count("[✅]") == 7
    assert "[❌]" not in out
    assert "code committed to git" in out
    assert "single git commit across all steps" in out
    assert "all artifact paths in tracked directories" in out
    assert "lineage saved on glaas.ai" in out
    # all green -> no warning
    assert "may not reproduce" not in out


def test_failed_checks_expand_with_exception():
    out = render_report(_full_report(committed=False, on_glaas=False))
    assert "Reproducibility — 5/7" in out
    assert out.count("[❌]") == 2
    assert out.count("[✅]") == 5
    assert "[❌] code committed to git" in out
    assert "→ run outside a git repo" in out  # the exception detail
    assert "may not reproduce as recorded" in out


def test_untracked_paths_fail_with_gitkeep_hint():
    out = render_report(_full_report(untracked_paths=["results/model.pt", "/tmp/x"]))
    assert "[❌] all artifact paths in tracked directories" in out
    assert "roar status --untracked-dirs" in out
    assert ".gitkeep" in out
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
        untracked_paths=[],
        on_glaas=False,
        na={"on_glaas": "dry run — nothing published yet"},
    )
    out = render_report(report)
    assert "[-] lineage saved on glaas.ai" in out
    assert "dry run — nothing published yet" in out
    # n/a excluded from the denominator: 6 applicable checks, all green
    assert "Reproducibility — 6/6" in out
    # an n/a item is not a failure, so no warning
    assert "may not reproduce" not in out


def test_check_keys_are_stable_and_complete():
    keys = [c.key for c in _full_report().checks]
    assert keys == [
        "committed",
        "single_commit",
        "pushed",
        "inputs_sourced",
        "paths_tracked",
        "runtime",
        "on_glaas",
    ]


def test_reproduce_report_omits_receipt_only_checks():
    """reproduce supplies neither on_glaas nor untracked_paths, so those
    register/put receipt checks are omitted — its punchlist carries only the
    checks that bear on whether the reproduction can run."""
    report = build_report(committed=True, pushed=True, runtime_ok=True, unsourced_paths=[])
    keys = [c.key for c in report.checks]
    assert keys == ["committed", "single_commit", "pushed", "inputs_sourced", "runtime"]
    out = render_report(report, title="Reproducibility (as recorded)")
    assert "lineage saved on glaas.ai" not in out
    assert "all artifact paths in tracked directories" not in out


def test_dir_will_exist_on_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # A directory with a tracked file survives a clean checkout.
    (repo / "data").mkdir()
    (repo / "data" / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(repo), "add", "data/.gitkeep"], check=True)
    # An untracked dir (only ever held gitignored/loose files) does not.
    (repo / "results").mkdir()
    (repo / "results" / "model.pt").write_text("x")  # untracked

    root = _resolve_repo_root(repo)
    assert root is not None
    assert _dir_will_exist_on_checkout(str(repo / "data"), root) is True
    assert _dir_will_exist_on_checkout(str(repo / "results"), root) is False
    assert _dir_will_exist_on_checkout(str(repo), root) is True  # repo root always exists
    # A path outside the repo is never recreated by checkout.
    assert _dir_will_exist_on_checkout(str(tmp_path / "elsewhere"), root) is False
    # Not in a git repo at all.
    assert _dir_will_exist_on_checkout(str(tmp_path), None) is False


def test_untracked_artifact_dirs_none_outside_a_repo(tmp_path: Path) -> None:
    """Not in a git repo -> the check doesn't apply (returns None), so callers
    omit the box rather than flagging every artifact. tmp_path is not a repo."""
    assert untracked_artifact_dirs(tmp_path / ".roar", tmp_path) is None


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
