"""Tests for ``emit_dirty_outputs_warning`` — the end-of-run warning."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from roar.application.run.output_followup import emit_dirty_outputs_warning
from roar.presenters.terminal import TerminalCaps


def _plain_caps() -> TerminalCaps:
    """ANSI off so assertions can grep the raw string."""
    return TerminalCaps(is_tty=False, can_color=False, can_emoji=False, width=80)


@pytest.fixture
def clean_porcelain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty `git status --porcelain` → tree is clean."""
    monkeypatch.setattr(
        subprocess,
        "check_output",
        MagicMock(return_value=""),
    )


@pytest.fixture
def hints_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `hints.enabled = True` regardless of project config."""
    monkeypatch.setattr(
        "roar.integrations.config.config_get",
        lambda key, **_kwargs: True if key == "hints.enabled" else None,
    )


def _with_porcelain(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(
        subprocess,
        "check_output",
        MagicMock(return_value=output),
    )


# ---------------------------------------------------------------------------
# Silent paths
# ---------------------------------------------------------------------------


def test_silent_when_tree_is_clean(
    tmp_path: Path, hints_enabled: None, clean_porcelain: None
) -> None:
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    assert buf.getvalue() == ""


def test_silent_when_quiet(tmp_path: Path, hints_enabled: None) -> None:
    """`quiet=True` short-circuits before any git call."""
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=True)
    assert buf.getvalue() == ""


def test_silent_when_hints_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "roar.integrations.config.config_get",
        lambda key, **_kwargs: False if key == "hints.enabled" else None,
    )
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    assert buf.getvalue() == ""


def test_silent_when_git_unavailable(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "check_output", MagicMock(side_effect=FileNotFoundError()))
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# Active paths
# ---------------------------------------------------------------------------


def test_warning_when_single_output_will_block(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_porcelain(monkeypatch, "?? model.pkl\n")
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    out = buf.getvalue()
    assert "warning: 1 output makes this repo dirty" in out
    assert "block the next `roar run`" in out
    assert "Add to .gitignore or commit." in out
    assert "echo 'model.pkl' >> .gitignore" in out
    assert "git add .gitignore && git commit" in out


def test_warning_pluralizes_for_multiple_outputs(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_porcelain(monkeypatch, "?? a.txt\n?? b.txt\n")
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    out = buf.getvalue()
    assert "warning: 2 outputs make this repo dirty" in out


def test_warning_uses_gitignore_pattern_when_threshold_hit(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_porcelain(monkeypatch, "?? a.pkl\n?? b.pkl\n?? c.pkl\n")
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    out = buf.getvalue()
    assert "warning: 3 outputs make this repo dirty" in out
    assert "echo '*.pkl' >> .gitignore" in out
    assert "(covers all 3)" in out


def test_warning_ignores_tracked_modifications(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracked-modified paths aren't outputs — they're code changes the
    user is in the middle of. Skip them in the end-of-run warning."""
    _with_porcelain(monkeypatch, " M train.py\n?? model.pkl\n")
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    out = buf.getvalue()
    assert "warning: 1 output makes" in out
    assert "model.pkl" in out
    assert "train.py" not in out


def test_warning_silent_when_only_tracked_modifications(
    tmp_path: Path, hints_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No untracked paths → nothing to warn about as an output."""
    _with_porcelain(monkeypatch, " M train.py\n")
    buf = io.StringIO()
    emit_dirty_outputs_warning(repo_root=tmp_path, stream=buf, caps=_plain_caps(), quiet=False)
    assert buf.getvalue() == ""


# -- emit_unsourced_inputs_nudge --------------------------------------------


def _summary(*paths):
    from roar.application.query.results import InputArtifactSummary, InputsSummary

    return InputsSummary(
        target_ref="@1",
        is_root=False,
        artifacts=[InputArtifactSummary(p, p, 0, unsourced=True) for p in paths],
    )


def test_nudge_warns_when_run_read_unsourced_inputs(
    monkeypatch: pytest.MonkeyPatch, hints_enabled: None, tmp_path: Path
) -> None:
    from roar.application.run import output_followup

    # The function imports build_inputs_summary locally, so patch it at source.
    monkeypatch.setattr(
        "roar.application.query.inputs.build_inputs_summary",
        lambda req: _summary("/data/events.csv"),
    )
    buf = io.StringIO()
    output_followup.emit_unsourced_inputs_nudge(
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        job_ref="@1",
        stream=buf,
        caps=_plain_caps(),
        quiet=False,
    )
    out = buf.getvalue()
    assert "1 input nothing tracked produced" in out
    assert "roar inputs --unsourced @1" in out


def test_nudge_silent_when_all_sourced(
    monkeypatch: pytest.MonkeyPatch, hints_enabled: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "roar.application.query.inputs.build_inputs_summary",
        lambda req: _summary(),  # no unsourced inputs
    )
    from roar.application.run import output_followup

    buf = io.StringIO()
    output_followup.emit_unsourced_inputs_nudge(
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        job_ref="@1",
        stream=buf,
        caps=_plain_caps(),
        quiet=False,
    )
    assert buf.getvalue() == ""


def test_nudge_silent_in_quiet_mode(tmp_path: Path) -> None:
    from roar.application.run import output_followup

    buf = io.StringIO()
    output_followup.emit_unsourced_inputs_nudge(
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        job_ref="@1",
        stream=buf,
        caps=_plain_caps(),
        quiet=True,
    )
    assert buf.getvalue() == ""
