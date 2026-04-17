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


def _short_hash(file_info: dict, color: bool) -> str:
    """Return a dim short hash string like 'blake3:a1b2c3d4…' for the first hash."""
    hashes = file_info.get("hashes", [])
    if not hashes:
        return ""
    h = hashes[0]
    algo = h.get("algorithm", "?")
    digest = h.get("digest", "")[:12]
    return style(f"{algo}:{digest}…", "dim", enabled=color)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


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
        proxy = "on" if proxy_active else "off"
        verb = style("tracing", "bold", enabled=self._caps.can_color)
        params = style(
            f"(tracer:{b} proxy:{proxy} sync:off)",
            "dim",
            enabled=self._caps.can_color,
        )
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

    def hashed(self, n_artifacts: int, total_bytes: int, duration: float) -> None:
        """Announce that hashing is complete with throughput."""
        if self._quiet or self._caps.pipe_mode:
            return
        verb = style("hashed", "bold", enabled=self._caps.can_color)
        noun = "artifact" if n_artifacts == 1 else "artifacts"
        if duration > 0 and total_bytes > 0:
            mbps = (total_bytes / 1024 / 1024) / duration
            throughput = style(f" · {mbps:.1f}MB/s", "dim", enabled=self._caps.can_color)
        else:
            throughput = ""
        self._emit_lifecycle(f"{verb} {n_artifacts} {noun}{throughput}")

    def lineage_captured(self) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        verb = style("lineage captured:", "bold", enabled=self._caps.can_color)
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
        breakdown = style(
            f"(trace {self._fmt_duration(trace_duration)} + "
            f"post {self._fmt_duration(post_duration)})",
            "dim",
            enabled=self._caps.can_color,
        )
        self._emit_lifecycle(f"{verb_styled} {breakdown}")

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
        c = self._caps.can_color
        w = self._caps.width

        # Layout widths.
        HASH_W = 8
        PARENT_OVERHEAD = 3  # " [" + "]"
        ARROW_W = 4  # " -> " or " →  "
        MARGIN = 4  # "· " + padding
        fixed = MARGIN + PARENT_OVERHEAD + HASH_W + ARROW_W + HASH_W + ARROW_W
        remaining = max(20, w - fixed - 2)
        path_w = min(24, remaining // 2)
        out_w = min(24, remaining - path_w)

        arrow = (
            style("→ ", "dim", enabled=c) if self._caps.can_emoji else style("> ", "dim", enabled=c)
        )
        blank_arrow = "  "
        job_color = "yellow"

        inputs = result.inputs
        outputs = result.outputs
        n_in = len(inputs)
        n_out = len(outputs)
        per_col = 4

        # -- header --
        self._emit_summary()
        in_hdr = style(f"Inputs ({n_in})", "dim", enabled=c)
        parent_hdr = style("[Parent  ]", "dim", enabled=c)
        job_hdr = style("Job", "dim", enabled=c)
        out_hdr = style(f"Outputs ({n_out})", "dim", enabled=c)
        # Assemble header: Inputs (N)   [Parent  ]  ->  Job        ->  Outputs (M)
        in_hdr_text = f"Inputs ({n_in})"
        in_pad = max(0, path_w - len(in_hdr_text))
        hdr = (
            f"{in_hdr}{' ' * in_pad} {parent_hdr} {arrow}"
            f"{job_hdr}{' ' * max(0, HASH_W - 3)} {arrow}"
            f"{out_hdr}"
        )
        self._emit_summary(hdr)

        # -- data rows --
        shown_in = min(n_in, per_col - 1) if n_in > per_col else min(n_in, per_col)
        shown_out = min(n_out, per_col - 1) if n_out > per_col else min(n_out, per_col)
        max_rows = max(
            shown_in + (1 if n_in > shown_in else 0), shown_out + (1 if n_out > shown_out else 0), 1
        )

        for i in range(max_rows):
            # Input path + parent.
            in_hash_line = ""
            if i < shown_in:
                inp = inputs[i]
                ipath = _truncate(_basename(inp["path"]), path_w)
                # Parent job UID — not yet plumbed; show "--" placeholder.
                parent = style("[--      ]", "dim", enabled=c)
                in_hash_line = _short_hash(inp, c)
            elif i == shown_in and n_in > shown_in:
                ipath = style(f"… and {n_in - shown_in} more", "dim", "italic", enabled=c)
                parent = " " * 10
            else:
                ipath = ""
                parent = " " * 10

            # Job column (only on first data row).
            if i == 0:
                job_val = style(result.job_uid, "bold", job_color, enabled=c)
                a = arrow
            else:
                job_val = " " * HASH_W
                a = blank_arrow

            # Output path.
            out_hash_line = ""
            if i < shown_out:
                opath = _truncate(_basename(outputs[i]["path"]), out_w)
                out_hash_line = _short_hash(outputs[i], c)
            elif i == shown_out and n_out > shown_out:
                opath = style(f"… and {n_out - shown_out} more", "dim", "italic", enabled=c)
            else:
                opath = ""

            ipath_vis = _visible_len(ipath)
            ipad = max(0, path_w - ipath_vis)
            job_vis = _visible_len(job_val)
            jpad = max(0, HASH_W - job_vis)

            line = f"{ipath}{' ' * ipad} {parent} {a}{job_val}{' ' * jpad} {a}{opath}"
            self._emit_summary(line)

            # Sub-line with short hashes (dim, indented under path).
            if in_hash_line or out_hash_line:
                in_sub = in_hash_line if in_hash_line else ""
                in_sub_vis = _visible_len(in_sub)
                in_sub_pad = max(0, path_w - in_sub_vis)
                # Blank parent + blank arrow + blank job + blank arrow = filler for middle cols.
                mid_filler = " " * 10 + " " + blank_arrow + " " * HASH_W + " " + blank_arrow
                out_sub = out_hash_line if out_hash_line else ""
                self._emit_summary(f"{in_sub}{' ' * in_sub_pad} {mid_filler}{out_sub}")

        # -- metadata lines --
        self._emit_summary()

        # Git info.
        if result.git_branch or result.git_short_commit:
            branch = result.git_branch or "?"
            commit = result.git_short_commit or "?"
            clean = "clean" if result.git_clean else "dirty"
            git_label = style("git:", "bold", enabled=c)
            git_val = style(f" {branch} @ {commit} {clean}", "dim", enabled=c)
            self._emit_summary(f"{git_label}{git_val}")

        # Env summary.
        parts = []
        if result.pip_count:
            parts.append(f"{result.pip_count} pip")
        if result.dpkg_count:
            parts.append(f"{result.dpkg_count} dpkg")
        if result.env_count:
            parts.append(f"{result.env_count} vars")
        if parts:
            env_label = style("env:", "bold", enabled=c)
            env_val = style(f" {' • '.join(parts)}", "dim", enabled=c)
            self._emit_summary(f"{env_label}{env_val}")

        # DAG summary.
        if result.dag_jobs or result.dag_artifacts:
            dag_parts = []
            if result.dag_jobs:
                dag_parts.append(f"{result.dag_jobs} jobs")
            if result.dag_artifacts:
                dag_parts.append(f"{result.dag_artifacts} artifacts")
            if result.dag_depth:
                dag_parts.append(f"depth {result.dag_depth}")
            dag_label = style("DAG:", "bold", enabled=c)
            dag_val = style(f" {' • '.join(dag_parts)}", "dim", enabled=c)
            self._emit_summary(f"{dag_label}{dag_val}")

        # Inspect section.
        self._emit_summary()
        self._emit_summary(style("Inspect:", "bold", enabled=c))
        show_cmd = style(f"roar show --job {result.job_uid}", "blue", enabled=c)
        show_comment = style("  # details", "dim", enabled=c)
        self._emit_summary(f"  {show_cmd}{show_comment}")
        if result.interrupted and result.outputs:
            pop_cmd = style("roar pop", "blue", enabled=c)
            pop_comment = style("  # undo interrupted run", "dim", enabled=c)
            self._emit_summary(f"  {pop_cmd}{pop_comment}")
        else:
            dag_cmd = style("roar dag", "blue", enabled=c)
            dag_comment = style("  # full lineage", "dim", enabled=c)
            self._emit_summary(f"  {dag_cmd}{dag_comment}")
        self._emit_summary()


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
