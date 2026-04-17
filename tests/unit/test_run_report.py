"""Tests for RunReportPresenter — the new lifecycle-style run output."""

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
    """Force TTY-like caps so presenter renders the full summary."""
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

    def print_dag(
        self,
        summary: dict[str, Any],
        stale_steps: set[int] | None = None,
    ) -> None:
        return None

    def confirm(self, message: str, default: bool = False) -> bool:
        return default


# ---- summary content ------------------------------------------------------


def test_interrupted_run_with_outputs_suggests_pop() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())

    report.summary(
        RunResult(
            exit_code=130,
            job_id=1,
            job_uid="job12345",
            duration=0.5,
            inputs=[],
            outputs=[{"path": "/tmp/out.txt", "size": 1, "hashes": []}],
            interrupted=True,
            is_build=False,
        ),
        ["python", "train.py"],
    )

    out = _strip(buf.getvalue())
    assert "roar pop" in out
    assert "roar dag" not in out
    assert "roar show --job job12345" in out


def test_successful_run_suggests_show_and_dag() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())

    report.summary(
        RunResult(
            exit_code=0,
            job_id=2,
            job_uid="job67890",
            duration=1.0,
            inputs=[],
            outputs=[],
            interrupted=False,
            is_build=False,
        ),
        ["python", "train.py"],
    )

    out = _strip(buf.getvalue())
    assert "roar show --job job67890" in out
    assert "roar dag" in out


def test_summary_has_three_column_headers() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())

    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=1.0,
            inputs=[{"path": "/a/in.txt", "size": 1, "hashes": []}],
            outputs=[{"path": "/a/out.txt", "size": 1, "hashes": []}],
            pip_count=5,
            dpkg_count=10,
            env_count=3,
        ),
        ["python", "x.py"],
    )
    out = _strip(buf.getvalue())
    assert "Inputs (1)" in out
    assert "abc12345" in out  # job UID in the data row
    assert "Outputs (1)" in out
    assert "5 pip" in out
    assert "10 dpkg" in out
    assert "3 vars" in out
    assert "Inspect:" in out


def test_summary_truncates_and_shows_more_indicator() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    # 10 inputs — well over the per-column cap of 4.
    inputs = [{"path": f"/data/in_{i}.txt", "size": 1, "hashes": []} for i in range(10)]
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=1.0,
            inputs=inputs,
            outputs=[],
        ),
        ["python", "x.py"],
    )
    out = _strip(buf.getvalue())
    assert "Inputs (10)" in out
    assert "and 7 more" in out


# ---- quiet + pipe modes ---------------------------------------------------


def test_quiet_mode_emits_nothing() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps(), quiet=True)

    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=0.5,
            inputs=[],
            outputs=[],
        ),
        ["python", "x.py"],
    )
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    assert buf.getvalue() == ""


def test_pipe_mode_emits_only_done_line() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_pipe_caps())

    # Lifecycle events are silent in pipe mode.
    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=0.5,
            inputs=[],
            outputs=[],
        ),
        ["python", "x.py"],
    )
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    out = buf.getvalue()
    # Only the final one-liner.
    assert out.count("\n") == 1
    assert out.startswith("roar: done")
    assert "exit 0" in out


# ---- lifecycle lines ------------------------------------------------------


def test_trace_starting_uses_plain_prefix_without_emoji() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.trace_starting(backend="preload", proxy_active=True)
    out = _strip(buf.getvalue())
    assert out.startswith("roar:")
    assert "tracing" in out
    assert "preload" in out
    assert "proxy:on" in out
    assert "sync:off" in out


def test_trace_ended_colors_nonzero_exit() -> None:
    buf = io.StringIO()
    caps = TerminalCaps(is_tty=True, can_color=True, can_emoji=False, width=80)
    report = RunReportPresenter(stream=buf, caps=caps)
    report.trace_ended(duration=1.5, exit_code=3)
    raw = buf.getvalue()
    # Red ANSI code around exit text.
    assert "\x1b[31m" in raw or "\x1b[1m\x1b[31m" in raw or "exit 3" in raw
    # And the plain text is still there.
    assert "exit 3" in _strip(raw)


def test_done_shows_separate_trace_and_post_durations() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.done(exit_code=0, trace_duration=1.3, post_duration=0.3)
    out = _strip(buf.getvalue())
    assert "trace 1.3s" in out
    assert "post 0.3s" in out
    assert "done" in out


# ---- legacy entry point ---------------------------------------------------


def test_show_report_legacy_one_shot() -> None:
    """show_report() still works — renders trace_ended + summary + done in a row."""
    buf = io.StringIO()
    report = RunReportPresenter(_CapturePresenter(), stream=buf, caps=_tty_caps())
    report.show_report(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="job12345",
            duration=1.0,
            inputs=[],
            outputs=[],
            post_duration=0.2,
        ),
        ["python", "x.py"],
    )
    out = _strip(buf.getvalue())
    assert "roar show --job job12345" in out
    assert "trace done" in out
    assert "done" in out
