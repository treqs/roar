"""Run report presenter - minimalist narration-style output.

Every status line is narrated by 🦖.  The lineage detail block uses
``·`` (middle dot) as a line prefix with 3-char category labels.

Color tokens (all in terminal.py, no raw ANSI here):
  status_green  - 🦖 lines, ``exit 0``, ``clean``
  warn_amber    - ``dirty`` git, non-zero exit
  command_blue  - suggested command text
  dim           - prefixes, labels, flags, comments, timing
  bold          - current job hash (no hue)
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from typing import IO

from ..core.interfaces.presenter import IPresenter
from ..core.models.run import RunResult
from .spinner import BRAILLE_FRAMES, CLOCK_FRAMES, Spinner
from .terminal import TerminalCaps, detect, style

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HASH_W = 8
_SMALL_RUN = 5  # skip transient hashing progress below this count


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _pad(text: str, width: int) -> str:
    vis = _visible_len(text)
    return text + " " * max(0, width - vis)


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "?"
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


class _NullProgress:
    def advance(self, delta: int = 1) -> None:
        pass

    def set_count(self, count: int) -> None:
        pass

    def update(self, message: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Presenter
# ---------------------------------------------------------------------------


class RunReportPresenter:
    """Minimalist narration-style output for ``roar run``."""

    def __init__(
        self,
        presenter: IPresenter | None = None,
        *,
        stream: IO | None = None,
        caps: TerminalCaps | None = None,
        quiet: bool = False,
    ) -> None:
        self._out = presenter
        self._stream = stream if stream is not None else sys.stderr
        self._caps = caps if caps is not None else detect(self._stream)
        self._quiet = quiet

    # ---- lifecycle events -------------------------------------------------

    def trace_starting(self, backend: str | None, proxy_active: bool) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        flags = style(
            f"tracer:{backend or 'auto'} proxy:{'on' if proxy_active else 'off'} sync:off",
            "dim",
            enabled=c,
        )
        self._trex(f"tracing {self._dim_sep()}{flags}")

    def trace_ended(self, duration: float, exit_code: int, backend: str | None = None) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        parts = ["trace done"]
        if backend:
            parts[0] += style(f" [{backend}]", "dim", enabled=c)
        parts.append(self._fmt_dur(duration))
        exit_s = f"exit {exit_code}"
        if exit_code == 0:
            exit_s = style(exit_s, "status_green", enabled=c)
        else:
            exit_s = style(exit_s, "warn_amber", "bold", enabled=c)
        parts.append(exit_s)
        self._trex(f" {self._dim_sep()}".join(parts))

    @contextmanager
    def hashing(self, total: int | None = None):
        if self._quiet or self._caps.pipe_mode:
            yield _NullProgress()
            return
        # Skip transient spinner for small runs.
        if total is not None and total < _SMALL_RUN:
            yield _NullProgress()
            return
        prefix = "🦖 " if self._caps.can_emoji else "roar: "
        frames = CLOCK_FRAMES if self._caps.can_emoji else BRAILLE_FRAMES
        with Spinner("hashing", prefix=prefix, frames=frames, interval=0.1) as sp:
            yield sp

    def hashed(self, n_artifacts: int, total_bytes: int, duration: float) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        text = f"hashed {_plural(n_artifacts, 'artifact')}"
        if duration > 0 and total_bytes > 0:
            mbps = (total_bytes / 1024 / 1024) / duration
            text += f" {self._dim_sep()}{style(f'{mbps:.1f} MB/s', 'dim', enabled=c)}"
        self._trex(text)

    def lineage_captured(self) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        self._trex("lineage captured:")

    def summary(self, result: RunResult, command: list[str]) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        self._render_summary(result)

    def done(self, *, exit_code: int, trace_duration: float, post_duration: float) -> None:
        if self._quiet:
            return
        if self._caps.pipe_mode:
            total = trace_duration + post_duration
            self._print(
                f"roar: done · {self._fmt_dur(total)} "
                f"(trace {self._fmt_dur(trace_duration)} + "
                f"post {self._fmt_dur(post_duration)}, exit {exit_code})"
            )
            return
        c = self._caps.can_color
        timing = style(
            f"trace {self._fmt_dur(trace_duration)} + post {self._fmt_dur(post_duration)}",
            "dim",
            enabled=c,
        )
        self._trex(f"done {self._dim_sep()}{timing}")

    # ---- backward-compat one-shot ----------------------------------------

    def show_report(self, result: RunResult, command: list[str], quiet: bool = False) -> None:
        if quiet or self._quiet:
            return
        if self._caps.pipe_mode:
            self.done(
                exit_code=result.exit_code,
                trace_duration=result.duration,
                post_duration=result.post_duration,
            )
            return
        self.trace_ended(result.duration, result.exit_code)
        self.lineage_captured()
        self._render_summary(result)
        self.done(
            exit_code=result.exit_code,
            trace_duration=result.duration,
            post_duration=result.post_duration,
        )

    # ---- stale warnings (unchanged) --------------------------------------

    def show_stale_warnings(
        self,
        stale_upstream: list[int],
        stale_downstream: list[int],
        is_build: bool = False,
    ) -> None:
        if not (stale_upstream or stale_downstream) or self._out is None:
            return
        if stale_upstream:
            self._out.print("")
            step_refs = ", ".join(f"@{s}" for s in stale_upstream)
            self._out.print(f"Warning: This job consumed stale inputs from: {step_refs}")
            self._out.print("The upstream steps were re-run but this step used old outputs.")
            self._out.print("Consider re-running this step after updating upstream.")
        if stale_downstream:
            self._out.print("")
            step_prefix = "B" if is_build else ""
            step_refs = ", ".join(f"@{step_prefix}{s}" for s in stale_downstream)
            self._out.print(f"Warning: Downstream steps are stale: {step_refs}")
            self._out.print("Run these steps to update them, or use 'roar dag' to see full status.")

    def show_upstream_stale_warning(self, step_num: int, upstream_stale: list[int]) -> bool:
        if self._out is None:
            return True
        step_refs = ", ".join(f"@{s}" for s in upstream_stale)
        self._out.print(f"Warning: Step @{step_num} depends on stale upstream steps: {step_refs}")
        self._out.print(
            "The upstream steps have been re-run more recently than their outputs were consumed."
        )
        self._out.print("")
        return self._out.confirm("Run anyway?", default=False)

    # ---- internal --------------------------------------------------------

    def _print(self, line: str = "") -> None:
        print(line, file=self._stream, flush=True)

    def _trex(self, text: str) -> None:
        """Emit a 🦖-prefixed status line in STATUS_GREEN."""
        c = self._caps.can_color
        prefix = "🦖" if self._caps.can_emoji else "roar:"
        self._print(
            f"{style(prefix, 'status_green', enabled=c)} {style(text, 'status_green', enabled=c)}"
        )

    def _detail(self, label: str, content: str) -> None:
        """Emit a ``·  label  content`` detail line."""
        c = self._caps.can_color
        prefix = style("·", "dim", enabled=c)
        lbl = style(f"{label:<3}", "dim", enabled=c)
        self._print(f"{prefix}  {lbl}  {content}")

    def _detail_blank(self) -> None:
        c = self._caps.can_color
        self._print(style("·", "dim", enabled=c))

    def _dim_sep(self) -> str:
        return style("· ", "dim", enabled=self._caps.can_color)

    def _fmt_dur(self, seconds: float) -> str:
        if seconds < 0.1:
            return f"{max(1, round(seconds * 1000))}ms"
        return f"{seconds:.1f}s"

    # ---- summary block ---------------------------------------------------

    def _render_summary(self, result: RunResult) -> None:
        c = self._caps.can_color

        # i/o line: "2 inputs ← 2 prior jobs · 1 output"
        n_in = len(result.inputs)
        n_out = len(result.outputs)
        io_parts = []
        if n_in:
            in_text = _plural(n_in, "input")
            # Count unique prior (source) jobs.
            source_jobs = {
                inp.get("parent_job_uid") for inp in result.inputs if inp.get("parent_job_uid")
            }
            if source_jobs:
                in_text += f" ← {_plural(len(source_jobs), 'prior job')}"
            io_parts.append(in_text)
        if n_out:
            io_parts.append(_plural(n_out, "output"))
        if io_parts:
            self._detail("i/o", f" {self._dim_sep()}".join(io_parts))

        # job line — bold hash, no hue.
        job_hash = style(result.job_uid, "bold", enabled=c)
        self._detail("job", job_hash)

        # git line.
        if result.git_branch or result.git_short_commit:
            branch = result.git_branch or "?"
            commit = result.git_short_commit or "?"
            if result.git_clean:
                state = style("clean", "status_green", enabled=c)
            else:
                state = style("dirty", "warn_amber", enabled=c)
            self._detail("git", f"{branch} @ {commit} {self._dim_sep()}{state}")

        # env line — pip/dpkg/vars are category labels, not countable nouns.
        env_parts = []
        if result.pip_count:
            env_parts.append(f"{result.pip_count} pip")
        if result.dpkg_count:
            env_parts.append(f"{result.dpkg_count} dpkg")
        if result.env_count:
            env_parts.append(_plural(result.env_count, "var"))
        if env_parts:
            self._detail("env", f" {self._dim_sep()}".join(env_parts))

        # dag line.
        if result.dag_jobs or result.dag_artifacts:
            dag_parts = []
            if result.dag_jobs:
                dag_parts.append(_plural(result.dag_jobs, "job"))
            if result.dag_artifacts:
                dag_parts.append(_plural(result.dag_artifacts, "artifact"))
            if result.dag_depth:
                dag_parts.append(f"depth {result.dag_depth}")
            self._detail("dag", f" {self._dim_sep()}".join(dag_parts))

        # Blank separator + suggested command.
        self._detail_blank()
        cmd_text = style(f"roar show --job {result.job_uid}", "command_blue", enabled=c)
        comment = style("# details", "dim", enabled=c)
        self._print(
            f"{style('·', 'dim', enabled=c)}  {style('$', 'dim', enabled=c)} {cmd_text}    {comment}"
        )
