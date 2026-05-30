"""Tests for RunReportPresenter — minimalist narration-style output."""

from __future__ import annotations

import io
import re
from typing import Any

from roar.core.models.run import RunResult
from roar.presenters.run_report import RunReportPresenter
from roar.presenters.terminal import TerminalCaps

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return ANSI_RE.sub("", s)


def _tty_caps(width: int = 120) -> TerminalCaps:
    return TerminalCaps(is_tty=True, can_color=False, can_emoji=False, width=width)


def _pipe_caps() -> TerminalCaps:
    return TerminalCaps(is_tty=False, can_color=False, can_emoji=False, width=80)


class _CapturePresenter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)

    def print_error(self, message: str) -> None:
        self.messages.append(message)

    def print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        return None

    def print_job(self, job: dict[str, Any], verbose: bool = False) -> None:
        return None

    def print_artifact(self, artifact: dict[str, Any]) -> None:
        return None

    def print_dag(self, summary: dict[str, Any], stale_steps: set[int] | None = None) -> None:
        return None

    def confirm(self, message: str, default: bool = False) -> bool:
        return default


def _make_result(**overrides: Any) -> RunResult:
    defaults: dict[str, Any] = {
        "exit_code": 0,
        "job_id": 1,
        "job_uid": "f3fba717",
        "duration": 1.0,
        "inputs": [],
        "outputs": [],
    }
    defaults.update(overrides)
    return RunResult(**defaults)


# ---- summary detail lines -------------------------------------------------


def test_io_line_counts_inputs_and_outputs() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        _make_result(
            inputs=[
                {"path": "/a/in1.txt", "hashes": []},
                {"path": "/a/in2.txt", "hashes": []},
            ],
            outputs=[{"path": "/a/out.txt", "hashes": []}],
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "i/o" in out
    assert "2 inputs" in out
    assert "1 output" in out


def test_job_line_shows_bold_hash() -> None:
    buf = io.StringIO()
    caps = TerminalCaps(is_tty=True, can_color=True, can_emoji=False, width=80)
    report = RunReportPresenter(stream=buf, caps=caps)
    report.summary(_make_result(job_uid="f3fba717"), [])
    raw = buf.getvalue()
    assert "f3fba717" in _strip(raw)
    # Bold ANSI code wraps the hash.
    assert "\x1b[1m" in raw


def test_git_line_clean() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        _make_result(git_branch="main", git_short_commit="10c570b", git_clean=True),
        [],
    )
    out = _strip(buf.getvalue())
    assert "git" in out
    assert "main @ 10c570b" in out
    assert "clean" in out


def test_git_line_dirty_uses_amber() -> None:
    buf = io.StringIO()
    caps = TerminalCaps(is_tty=True, can_color=True, can_emoji=False, width=80)
    report = RunReportPresenter(stream=buf, caps=caps)
    report.summary(
        _make_result(git_branch="main", git_short_commit="abc", git_clean=False),
        [],
    )
    raw = buf.getvalue()
    assert "dirty" in _strip(raw)
    # warn_amber = ANSI 256-color 172
    assert "\x1b[38;5;172m" in raw


def test_env_line() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(_make_result(pip_count=9, dpkg_count=10, env_count=3), [])
    out = _strip(buf.getvalue())
    assert "env" in out
    assert "9 pip" in out
    assert "10 dpkg" in out
    assert "3 var" in out


def test_dag_line_singular() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(_make_result(dag_jobs=1, dag_artifacts=1, dag_depth=1), [])
    out = _strip(buf.getvalue())
    assert "1 job" in out
    assert "1 artifact" in out
    assert "artifacts" not in out


def test_summary_no_longer_embeds_show_suggestion() -> None:
    """The old `· $ roar show --job …    # details` row inside the
    summary block moved out to a `hint:` line after `done()`. Make sure
    nothing puts it back."""
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(_make_result(job_uid="f3fba717"), [])
    out = _strip(buf.getvalue())
    assert "$ roar show" not in out
    assert "# details" not in out


def test_next_steps_hint_prefers_step_form() -> None:
    """When the step number is known, the register suggestion uses the
    `@N` form — shorter and more discoverable than the bare UID."""
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.next_steps_hint(_make_result(job_uid="abc12345", step_number=3))
    out = _strip(buf.getvalue())
    assert "hint: next: roar show --job abc12345" in out
    assert "roar dag" in out
    assert "roar register @3" in out


def test_next_steps_hint_falls_back_to_uid_form_when_no_step_number() -> None:
    """Some code paths build RunResult without step_number. The register
    suggestion still works — falls back to the UID form, which the
    target resolver also accepts."""
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.next_steps_hint(_make_result(job_uid="abc12345", step_number=None))
    out = _strip(buf.getvalue())
    assert "roar register abc12345" in out
    # No bogus `@N` form when N isn't available.
    assert "roar register @" not in out


def test_next_steps_hint_explains_what_register_does() -> None:
    """The hint line is followed by a one-line explainer that tells the
    user (and any agent reading the output) what `roar register` does.
    Uses the same register_arg as the action line so the two stay in
    sync."""
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.next_steps_hint(_make_result(job_uid="abc12345", step_number=3))
    out = _strip(buf.getvalue())
    assert "'roar register @3' uploads lineage to glaas.ai" in out
    assert "reproduce it later" in out


def test_next_steps_hint_explainer_uses_uid_form_when_no_step_number() -> None:
    """When the action line falls back to the UID form, the explainer
    refers to the same UID — not a missing `@N`."""
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.next_steps_hint(_make_result(job_uid="abc12345", step_number=None))
    out = _strip(buf.getvalue())
    assert "'roar register abc12345' uploads lineage to glaas.ai" in out
    assert "'roar register @" not in out


def test_next_steps_hint_appears_in_pipe_mode_for_agents_and_ci() -> None:
    """Pipe mode (non-TTY stderr) no longer suppresses the hint.

    Agents and CI logs that capture stderr should see the next-step
    nudge. Hints already live on stderr — stdout consumers don't need
    to be protected by suppression. ANSI styling still gates on
    ``caps.can_color`` so captured logs stay plain.
    """
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_pipe_caps())
    report.next_steps_hint(_make_result(job_uid="abc12345"))
    out = _strip(buf.getvalue())
    assert "hint: next: roar show --job abc12345" in out


def test_next_steps_hint_silent_in_quiet_mode() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps(), verbosity="quiet")
    report.next_steps_hint(_make_result(job_uid="abc12345"))
    assert buf.getvalue() == ""


def test_next_steps_hint_silent_when_hints_disabled() -> None:
    """`hints.enabled = false` in config suppresses the hint."""
    from unittest.mock import patch

    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    with patch("roar.integrations.config.config_get", return_value=False):
        report.next_steps_hint(_make_result(job_uid="abc12345"))
    assert buf.getvalue() == ""


def test_tmp_filtered_hint_fires_when_tmp_io_was_filtered() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.tmp_filtered_hint(_make_result(filter_counts={"tmp_files": 3}))
    out = _strip(buf.getvalue())
    assert "hint:" in out
    assert "3 /tmp files filtered" in out
    assert "filters.ignore_tmp_files" in out


def test_tmp_filtered_hint_singular() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.tmp_filtered_hint(_make_result(filter_counts={"tmp_files": 1}))
    assert "1 /tmp file filtered" in _strip(buf.getvalue())


def test_tmp_filtered_hint_silent_when_nothing_filtered() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.tmp_filtered_hint(_make_result(filter_counts={"tmp_files": 0}))
    assert buf.getvalue() == ""
    buf2 = io.StringIO()
    report2 = RunReportPresenter(stream=buf2, caps=_tty_caps())
    report2.tmp_filtered_hint(_make_result())  # no filter_counts at all
    assert buf2.getvalue() == ""


def test_tmp_filtered_hint_silent_in_quiet_mode() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps(), verbosity="quiet")
    report.tmp_filtered_hint(_make_result(filter_counts={"tmp_files": 3}))
    assert buf.getvalue() == ""


def test_tmp_filtered_hint_silent_when_hints_disabled() -> None:
    from unittest.mock import patch

    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    with patch("roar.integrations.config.config_get", return_value=False):
        report.tmp_filtered_hint(_make_result(filter_counts={"tmp_files": 3}))
    assert buf.getvalue() == ""


# ---- lifecycle lines -------------------------------------------------------


def test_trace_starting() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.trace_starting(backend="preload", proxy_active=False)
    out = _strip(buf.getvalue())
    assert "tracing" in out
    assert "tracer:preload" in out
    assert "sync:off" in out


def test_trace_ended_success() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.trace_ended(duration=11.2, exit_code=0)
    out = _strip(buf.getvalue())
    assert "trace done" in out
    assert "11.2s" in out
    assert "exit 0" in out


def test_trace_ended_nonzero_exit() -> None:
    buf = io.StringIO()
    caps = TerminalCaps(is_tty=True, can_color=True, can_emoji=False, width=80)
    report = RunReportPresenter(stream=buf, caps=caps)
    report.trace_ended(duration=1.0, exit_code=1)
    raw = buf.getvalue()
    assert "exit 1" in _strip(raw)
    # warn_amber for non-zero exit
    assert "\x1b[38;5;172m" in raw


def test_hashed_singular() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.hashed(n_artifacts=1, total_bytes=1024 * 1024, duration=0.5)
    out = _strip(buf.getvalue())
    assert "1 artifact" in out
    assert "artifacts" not in out
    assert "MB/s" in out


def test_done_shows_timing_breakdown() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.done(exit_code=0, trace_duration=11.2, post_duration=0.6)
    out = _strip(buf.getvalue())
    assert "done" in out
    assert "trace 11.2s" in out
    assert "post 0.6s" in out


# ---- quiet + pipe modes ---------------------------------------------------


def test_quiet_mode_emits_nothing() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps(), quiet=True)
    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(_make_result(), [])
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    assert buf.getvalue() == ""


def test_quiet_mode_suppresses_stale_warnings() -> None:
    """`-q` on `roar run` strips all roar-emitted output, including the
    stale-input/output safety warnings. The user is trading the signal
    for a clean wrapped-command pipeline."""
    presenter = _CapturePresenter()
    buf = io.StringIO()
    report = RunReportPresenter(presenter, stream=buf, caps=_tty_caps(), quiet=True)
    report.show_stale_warnings(stale_upstream=[1, 2], stale_downstream=[5])
    assert presenter.messages == []
    assert buf.getvalue() == ""


def test_pipe_mode_emits_only_done_line() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_pipe_caps())
    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(_make_result(), [])
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    out = buf.getvalue()
    assert out.count("\n") == 1
    assert out.startswith("roar: done")


# ---- legacy one-shot -------------------------------------------------------


def test_show_report_legacy() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(_CapturePresenter(), stream=buf, caps=_tty_caps())
    report.show_report(_make_result(post_duration=0.2), [])
    out = _strip(buf.getvalue())
    assert "roar show --job f3fba717" in out
    assert "trace done" in out
    assert "done" in out
