"""Run report presenter.

Renders the lifecycle of a `roar run` as a series of brief, prefixed lines
followed by a three-column summary block. All output goes to stderr so the
user command's stdout remains clean for piping.

Design points:
* 🦖 prefix on lifecycle lines when emoji is supported, `roar:` otherwise.
* Middle-dot `·` prefix on the summary block lines (same idea as 🦖 but
  quieter — keeps the block visually tied to roar without repeating branding).
* Arrow between columns on the first data row only — direction of flow shown
  once, not on every row.
* Trace duration and post-processing duration reported separately so the
  overhead of roar is visible.
* When stderr is not a TTY (piped / redirected / captured), drop to a single
  one-line "done" summary.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

from ..core.interfaces.presenter import IPresenter
from ..core.models.run import RunResult
from .spinner import BRAILLE_FRAMES, CLOCK_FRAMES, Spinner
from .terminal import TerminalCaps, detect, style


def format_size(size_bytes: int | None) -> str:
    """Format file size in human-readable format."""
    if size_bytes is None:
        return "?"
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _basename(path: str) -> str:
    """Best-effort relative path for display; falls back to basename."""
    try:
        rel = os.path.relpath(path)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return os.path.basename(path) or path


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ColumnPlan:
    col_width: int
    gutter: int
    indent: int

    @property
    def total(self) -> int:
        return self.indent + 3 * self.col_width + 2 * self.gutter

    @classmethod
    def for_width(cls, width: int) -> _ColumnPlan:
        indent = 4  # 2 (margin dot + space) + 2 more for breathing room
        # Leave a few chars of right-hand slack.
        available = max(48, width - indent - 2)
        # Gutter holds " │ → " (or " | > ") = 5 chars; must stay in sync with
        # _format_row below.
        gutter = 5
        col = (available - 2 * gutter) // 3
        col = max(14, min(col, 24))
        return cls(col_width=col, gutter=gutter, indent=indent)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _pad(text: str, width: int) -> str:
    visible = len(text)
    if visible >= width:
        return text
    return text + " " * (width - visible)


# ---------------------------------------------------------------------------
# Presenter
# ---------------------------------------------------------------------------


class RunReportPresenter:
    """Formats and displays run lifecycle events and the final summary.

    The legacy entry point ``show_report(result, command, quiet)`` is preserved
    for backward compatibility; it renders the new one-shot summary. Callers
    that want lifecycle output (``trace_starting`` → ``trace_ended`` → hashing
    spinner → ``lineage_captured`` → ``summary`` → ``done``) can invoke those
    methods individually.
    """

    def __init__(
        self,
        presenter: IPresenter | None = None,
        *,
        stream: IO | None = None,
        caps: TerminalCaps | None = None,
        quiet: bool = False,
    ) -> None:
        # `presenter` is retained only so that ``show_stale_warnings`` can
        # continue to use it; new output always goes to stderr.
        self._out = presenter
        self._stream = stream if stream is not None else sys.stderr
        self._caps = caps if caps is not None else detect(self._stream)
        self._quiet = quiet

    # ---- lifecycle events -------------------------------------------------

    def trace_starting(self, backend: str | None, proxy_active: bool) -> None:
        """Announce that tracing is about to start."""
        if self._quiet or self._caps.pipe_mode:
            return
        b = backend or "auto"
        proxy = "proxy on" if proxy_active else "proxy off"
        verb = style("tracing", "bold", enabled=self._caps.can_color)
        params = style(f"with {b} ({proxy})", "dim", enabled=self._caps.can_color)
        self._emit_lifecycle(f"{verb} {params}")

    def trace_ended(self, duration: float, exit_code: int) -> None:
        """Announce that the traced command has exited."""
        if self._quiet or self._caps.pipe_mode:
            return
        verb = style("trace done", "bold", enabled=self._caps.can_color)
        dur = self._fmt_duration(duration)
        exit_text = f"exit {exit_code}"
        if exit_code != 0 and self._caps.can_color:
            exit_text = style(exit_text, "red", "bold", enabled=True)
        elif exit_code == 0 and self._caps.can_color:
            exit_text = style(exit_text, "dim", enabled=True)
        self._emit_lifecycle(f"{verb} · {dur} · {exit_text}")

    @contextmanager
    def hashing(self, total: int | None = None):
        """Context manager: render a spinner for the hashing/recording phase.

        *total*, if given, is displayed as "(N artifacts)" after the label —
        the counter does not currently update live during hashing, so we show
        the static total rather than a misleading "0/N" that never ticks.
        """
        if self._quiet or self._caps.pipe_mode:
            yield _NullProgress()
            return
        prefix = self._lifecycle_prefix()
        label = style("hashing", "bold", enabled=self._caps.can_color)
        if total:
            noun = "artifact" if total == 1 else "artifacts"
            count_str = style(f" ({total} {noun})", "dim", enabled=self._caps.can_color)
            label = f"{label}{count_str}"
        frames = CLOCK_FRAMES if self._caps.can_emoji else BRAILLE_FRAMES
        with Spinner(label, prefix=prefix, frames=frames, interval=0.1) as sp:
            yield sp

    def lineage_captured(self) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        verb = style("lineage captured", "bold", enabled=self._caps.can_color)
        self._emit_lifecycle(verb)

    def summary(self, result: RunResult, command: list[str]) -> None:
        """Render the three-column inputs/job/outputs block."""
        if self._quiet or self._caps.pipe_mode:
            return
        self._render_summary(result, command)

    def done(
        self,
        *,
        exit_code: int,
        trace_duration: float,
        post_duration: float,
    ) -> None:
        """Emit the final line. Falls back to a one-liner in pipe mode."""
        if self._quiet:
            return
        total = trace_duration + post_duration
        verb = "done" if exit_code == 0 else "failed"
        if self._caps.pipe_mode:
            # Minimal one-liner when piping.
            line = (
                f"roar: {verb} · {self._fmt_duration(total)} "
                f"(trace {self._fmt_duration(trace_duration)} + "
                f"post {self._fmt_duration(post_duration)}, exit {exit_code})"
            )
            print(line, file=self._stream, flush=True)
            return
        color = "green" if exit_code == 0 else "red"
        verb_styled = style(verb, "bold", color, enabled=self._caps.can_color)
        total_s = style(self._fmt_duration(total), "bold", enabled=self._caps.can_color)
        breakdown = style(
            f"(trace {self._fmt_duration(trace_duration)} + "
            f"post {self._fmt_duration(post_duration)})",
            "dim",
            enabled=self._caps.can_color,
        )
        self._emit_lifecycle(f"{verb_styled} · {total_s} {breakdown}")

    # ---- backward-compat one-shot ----------------------------------------

    def show_report(
        self,
        result: RunResult,
        command: list[str],
        quiet: bool = False,
    ) -> None:
        """Render the full lifecycle in one call using data in *result*.

        Used by application/run/execution.py when the run has already
        finished and we only have the RunResult to work with.
        """
        if quiet or self._quiet:
            return
        if self._caps.pipe_mode:
            self.done(
                exit_code=result.exit_code,
                trace_duration=result.duration,
                post_duration=result.post_duration,
            )
            return
        # The trace_starting / trace_ended / hashing / lineage_captured lines
        # are ideally emitted during the run itself. When this method is the
        # only entry point we skip those and render the meaningful tail.
        self.trace_ended(result.duration, result.exit_code)
        self.lineage_captured()
        self._render_summary(result, command)
        self.done(
            exit_code=result.exit_code,
            trace_duration=result.duration,
            post_duration=result.post_duration,
        )

    # ---- stale warnings (unchanged semantics) ----------------------------

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

    def show_upstream_stale_warning(
        self,
        step_num: int,
        upstream_stale: list[int],
    ) -> bool:
        if self._out is None:
            return True
        step_refs = ", ".join(f"@{s}" for s in upstream_stale)
        self._out.print(f"Warning: Step @{step_num} depends on stale upstream steps: {step_refs}")
        self._out.print(
            "The upstream steps have been re-run more recently than their outputs were consumed."
        )
        self._out.print("")
        return self._out.confirm("Run anyway?", default=False)

    # ---- internals -------------------------------------------------------

    def _lifecycle_prefix(self) -> str:
        return "🦖 " if self._caps.can_emoji else "roar: "

    def _summary_prefix(self) -> str:
        """Subtle line prefix for the summary block — dim middle-dot."""
        dot = "·" if self._caps.can_emoji else "."
        return style(f"{dot} ", "dim", enabled=self._caps.can_color)

    def _emit_lifecycle(self, message: str) -> None:
        prefix = self._lifecycle_prefix()
        color = self._caps.can_color
        brand = style(prefix.rstrip(), "magenta", enabled=color)
        print(f"{brand} {message}", file=self._stream, flush=True)

    def _emit_summary(self, line: str = "") -> None:
        if line:
            print(f"{self._summary_prefix()}{line}", file=self._stream, flush=True)
        else:
            print(self._summary_prefix().rstrip(), file=self._stream, flush=True)

    def _fmt_duration(self, seconds: float) -> str:
        # Keep it tight: sub-second in ms, else one decimal.
        if seconds < 0.1:
            ms = max(1, round(seconds * 1000))
            return f"{ms}ms"
        return f"{seconds:.1f}s"

    def _render_summary(self, result: RunResult, command: list[str]) -> None:
        plan = _ColumnPlan.for_width(self._caps.width)

        inputs = [_basename(f["path"]) for f in result.inputs]
        outputs = [_basename(f["path"]) for f in result.outputs]

        n_in = len(inputs)
        n_out = len(outputs)

        in_header = f"Inputs ({n_in})"
        job_header = f"Job  {result.job_uid}"
        out_header = f"Outputs ({n_out})"

        # Rows available per column after the header.
        per_col = 4

        # Left column rows (inputs, with "... and N more" if needed).
        in_rows: list[str] = []
        shown_in = min(n_in, per_col - 1) if n_in > per_col else min(n_in, per_col)
        for path in inputs[:shown_in]:
            in_rows.append(_truncate(path, plan.col_width))
        if n_in > shown_in:
            more = style(
                f"… and {n_in - shown_in} more",
                "dim",
                "italic",
                enabled=self._caps.can_color,
            )
            in_rows.append(more)

        # Middle column rows: package & env counts.
        job_rows = []
        if result.pip_count:
            job_rows.append(f"{result.pip_count} pip pkgs")
        if result.dpkg_count:
            job_rows.append(f"{result.dpkg_count} dpkg pkgs")
        if result.env_count:
            job_rows.append(f"{result.env_count} env vars")
        if not job_rows:
            job_rows.append(style("—", "dim", enabled=self._caps.can_color))

        # Right column rows (outputs).
        out_rows: list[str] = []
        shown_out = min(n_out, per_col - 1) if n_out > per_col else min(n_out, per_col)
        for path in outputs[:shown_out]:
            out_rows.append(_truncate(path, plan.col_width))
        if n_out > shown_out:
            more = style(
                f"… and {n_out - shown_out} more",
                "dim",
                "italic",
                enabled=self._caps.can_color,
            )
            out_rows.append(more)

        # -- emit --
        self._emit_summary()
        header_line = self._format_row(
            in_header, job_header, out_header, plan, arrow=False, dim_all=True
        )
        self._emit_summary(header_line)

        max_rows = max(len(in_rows), len(job_rows), len(out_rows), 1)
        for i in range(max_rows):
            left = in_rows[i] if i < len(in_rows) else ""
            mid = job_rows[i] if i < len(job_rows) else ""
            right = out_rows[i] if i < len(out_rows) else ""
            self._emit_summary(self._format_row(left, mid, right, plan, arrow=(i == 0)))

        self._emit_summary()
        show_cmd = style(
            f"roar show --job {result.job_uid}",
            "cyan",
            enabled=self._caps.can_color,
        )
        # Offer `roar pop` instead of `roar dag` when the run was interrupted
        # and left partial outputs behind (pop removes the partial job).
        if result.interrupted and result.outputs:
            next_cmd = style("roar pop", "cyan", enabled=self._caps.can_color)
        else:
            next_cmd = style("roar dag", "cyan", enabled=self._caps.can_color)
        self._emit_summary(show_cmd)
        self._emit_summary(next_cmd)
        self._emit_summary()

    def _format_row(
        self,
        left: str,
        mid: str,
        right: str,
        plan: _ColumnPlan,
        *,
        arrow: bool,
        dim_all: bool = False,
    ) -> str:
        """Render a single row with the three columns padded to plan widths.

        The gutter between columns is 5 chars: ``" │ → "`` on the first data
        row (arrow pointing into the next column), ``" │   "`` elsewhere.
        The vertical rule ``│`` (or ``|`` without UTF-8) runs the full height
        of the block, giving the columns clear visual separation.
        """
        color = self._caps.can_color and dim_all

        def styled(s: str) -> str:
            return style(s, "dim", enabled=color) if color and s else s

        left_visible = _visible_len(left)
        mid_visible = _visible_len(mid)
        right_visible = _visible_len(right)

        left_pad = max(0, plan.col_width - left_visible)
        mid_pad = max(0, plan.col_width - mid_visible)
        right_pad = max(0, plan.col_width - right_visible)

        # Gutter: space, rule, space, arrow-or-space, space  (5 chars visible).
        rule_char = "│" if self._caps.can_emoji else "|"
        arrow_char = "→" if self._caps.can_emoji else ">"
        rule = style(rule_char, "dim", enabled=self._caps.can_color)
        arrow_vis = arrow_char if arrow else " "
        gutter = f" {rule} {arrow_vis} "

        return (
            f"{styled(left)}{' ' * left_pad}{gutter}"
            f"{styled(mid)}{' ' * mid_pad}{gutter}"
            f"{styled(right)}{' ' * right_pad}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of a string ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


class _NullProgress:
    """No-op stand-in returned by Presenter.hashing() when in pipe mode."""

    def advance(self, delta: int = 1) -> None:
        pass

    def set_count(self, count: int) -> None:
        pass

    def update(self, message: str) -> None:
        pass
