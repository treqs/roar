"""Tests for RunReportPresenter — v7 section-based layout."""

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


# ---- section layout -------------------------------------------------------


def test_inputs_section_with_source_job() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=1.0,
            inputs=[
                {
                    "path": "/a/in.txt",
                    "size": 1,
                    "hashes": [{"algorithm": "blake3", "digest": "d328d068abcd1234"}],
                    "parent_job_uid": "8dc58ec2",
                },
            ],
            outputs=[{"path": "/a/out.txt", "size": 1, "hashes": []}],
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "Inputs (1)" in out
    assert "Hash" in out
    assert "Source Job" in out
    assert "d328d068" in out
    assert "8dc58ec2" in out


def test_outputs_section_no_source_job() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=1.0,
            inputs=[],
            outputs=[
                {
                    "path": "/a/out.bin",
                    "size": 100,
                    "hashes": [{"algorithm": "blake3", "digest": "b7ad9ea41234abcd"}],
                },
            ],
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "Outputs (1)" in out
    assert "b7ad9ea4" in out
    assert "Source Job" not in out.split("Outputs")[1]


def test_job_section_with_git_and_env() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="f3fba717",
            duration=1.0,
            inputs=[],
            outputs=[],
            git_branch="main",
            git_short_commit="10c570b",
            git_clean=True,
            pip_count=9,
            dpkg_count=10,
            env_count=3,
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "Job" in out
    assert "id" in out
    assert "f3fba717" in out
    assert "git" in out
    assert "main @ 10c570b" in out
    assert "clean" in out
    assert "env" in out
    assert "9 pip" in out
    assert "10 dpkg" in out
    assert "3 var" in out


def test_dag_section() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        RunResult(
            exit_code=0,
            job_id=1,
            job_uid="abc12345",
            duration=1.0,
            inputs=[],
            outputs=[],
            dag_jobs=4,
            dag_artifacts=1,
            dag_depth=2,
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "DAG" in out
    assert "4 jobs" in out
    assert "1 artifact" in out  # singular
    assert "depth 2" in out


def test_inspect_section_suggests_show_and_dag() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.summary(
        RunResult(exit_code=0, job_id=1, job_uid="abc12345", duration=1.0, inputs=[], outputs=[]),
        [],
    )
    out = _strip(buf.getvalue())
    assert "Inspect" in out
    assert "roar show --job abc12345" in out
    assert "# details" in out
    assert "roar dag" in out
    assert "# full lineage" in out


def test_interrupted_run_suggests_pop() -> None:
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
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "roar pop" in out
    assert "roar dag" not in out


def test_truncation_with_more_indicator() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    inputs = [{"path": f"/data/in_{i}.txt", "size": 1, "hashes": []} for i in range(10)]
    report.summary(
        RunResult(
            exit_code=0, job_id=1, job_uid="abc12345", duration=1.0, inputs=inputs, outputs=[]
        ),
        [],
    )
    out = _strip(buf.getvalue())
    assert "Inputs (10)" in out
    assert "and 6 more" in out


# ---- quiet + pipe modes ---------------------------------------------------


def test_quiet_mode_emits_nothing() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps(), quiet=True)
    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(
        RunResult(exit_code=0, job_id=1, job_uid="abc12345", duration=0.5, inputs=[], outputs=[]),
        [],
    )
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    assert buf.getvalue() == ""


def test_pipe_mode_emits_only_done_line() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_pipe_caps())
    report.trace_starting(backend="preload", proxy_active=False)
    report.trace_ended(duration=0.5, exit_code=0)
    report.lineage_captured()
    report.summary(
        RunResult(exit_code=0, job_id=1, job_uid="abc12345", duration=0.5, inputs=[], outputs=[]),
        [],
    )
    report.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
    out = buf.getvalue()
    assert out.count("\n") == 1
    assert out.startswith("roar: done")


# ---- lifecycle lines -------------------------------------------------------


def test_trace_starting_format() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.trace_starting(backend="preload", proxy_active=True)
    out = _strip(buf.getvalue())
    assert "tracing" in out
    assert "tracer:preload" in out
    assert "proxy:on" in out
    assert "sync:off" in out


def test_trace_ended_exit_before_duration() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.trace_ended(duration=11.2, exit_code=0)
    out = _strip(buf.getvalue())
    exit_pos = out.index("exit 0")
    dur_pos = out.index("11.2s")
    assert exit_pos < dur_pos  # exit code appears before duration


def test_hashed_line_singular() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.hashed(n_artifacts=1, total_bytes=1024 * 1024, duration=0.5)
    out = _strip(buf.getvalue())
    assert "1 artifact" in out
    assert "artifacts" not in out
    assert "MB/s" in out


def test_done_shows_trace_and_post() -> None:
    buf = io.StringIO()
    report = RunReportPresenter(stream=buf, caps=_tty_caps())
    report.done(exit_code=0, trace_duration=11.2, post_duration=0.6)
    out = _strip(buf.getvalue())
    assert "done" in out
    assert "trace 11.2s" in out
    assert "post 0.6s" in out


def test_lineage_uses_trex_emoji() -> None:
    buf = io.StringIO()
    caps = TerminalCaps(is_tty=True, can_color=False, can_emoji=True, width=80)
    report = RunReportPresenter(stream=buf, caps=caps)
    report.lineage_captured()
    assert "🦖" in buf.getvalue()


# ---- legacy one-shot -------------------------------------------------------


def test_show_report_legacy() -> None:
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
        [],
    )
    out = _strip(buf.getvalue())
    assert "roar show --job job12345" in out
    assert "trace done" in out
    assert "done" in out
