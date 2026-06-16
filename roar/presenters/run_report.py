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

import sys
from contextlib import contextmanager
from typing import IO, Literal

from ..core.interfaces.presenter import IPresenter
from ..core.models.run import RunResult
from .spinner import BRAILLE_FRAMES, CLOCK_FRAMES, Spinner
from .terminal import TerminalCaps, detect, style

Verbosity = Literal["quiet", "normal", "verbose", "debug"]

# Pretty labels for each filter category in normal-mode summary line and
# debug-mode listings. Order matters: this is the display order.
_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    ("system_reads", "system"),
    ("package_reads", "packages"),
    ("torch_cache", "torch cache"),
    ("tmp_files", "/tmp"),
    ("write_noise", "system-writes"),
    ("git_metadata", "git"),
    ("credential_files", "secrets"),
    # Kept for verbose/debug dropped-file listings; hidden from the compact
    # summary line (roar's own bookkeeping isn't user-meaningful there).
    ("roar_internal", "roar-internal"),
)

# Width of the detail-line label column. Sized to the widest label
# ("ignored i/o") so the value column stays aligned across all rows.
_DETAIL_LABEL_WIDTH = len("ignored i/o")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SMALL_RUN = 5  # skip transient hashing progress below this count


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        verbosity: Verbosity | None = None,
    ) -> None:
        self._out = presenter
        self._stream = stream if stream is not None else sys.stderr
        self._caps = caps if caps is not None else detect(self._stream)
        # `quiet=True` is a back-compat alias for verbosity="quiet" so old
        # callers still get the silent behavior. New callers should pass
        # verbosity directly.
        if verbosity is None:
            verbosity = "quiet" if quiet else "normal"
        self._verbosity: Verbosity = verbosity
        self._quiet = verbosity == "quiet"
        self._show_io_lists = verbosity in ("verbose", "debug")
        self._show_dropped_lists = verbosity == "debug"

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

    def trace_ended(self, duration: float, exit_code: int) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        parts = ["trace done"]
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
        if self._quiet:
            return
        # Pipe mode normally suppresses the decorative summary block, but
        # verbose/debug users explicitly asked for the file lists — those
        # are most useful in CI logs and redirected output, so honor them.
        if self._caps.pipe_mode and not self._show_io_lists:
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

    def next_steps_hint(self, result: RunResult) -> None:
        """Amber ``hint: next: …`` line printed after ``done()``.

        Restores the next-step breadcrumb that v0.2.10 had as a Next:
        block embedded in the summary — but as a single hint line.
        Goes to the presenter's stream (stderr by default), gated by
        ``_quiet`` and ``hints.enabled = false``. No TTY check: stdout
        consumers stay clean by virtue of the stream choice, and
        non-TTY consumers (CI logs, agents) still see the nudge. ANSI
        styling further gates on ``self._caps.can_color`` so captured
        logs stay plain.
        """
        if self._quiet:
            return
        try:
            from ..integrations.config import config_get

            if config_get("hints.enabled") is False:
                return
        except Exception:
            # If config can't be read, default to showing the hint —
            # the run output is dominated by stderr noise on a TTY,
            # and one extra line is harmless.
            pass

        c = self._caps.can_color
        # `roar register` accepts step references (`@N`) and job UIDs.
        # Prefer the `@N` form when we know the step number — shorter
        # and more discoverable. Falls back to the bare UID form (which
        # the resolver also classifies as a job_uid target) when the
        # step number isn't available on this RunResult.
        if result.step_number is not None:
            register_arg = f"@{result.step_number}"
        else:
            register_arg = result.job_uid
        # Use the `@N` alias for `show` too (shorter, matches the job line),
        # falling back to the bare UID when the step number isn't known.
        line = f"hint: next: roar show {register_arg}  ·  roar dag  ·  roar register {register_arg}"
        self._print(style(line, "warn_amber", enabled=c))

    def no_repo_hint(self, result: RunResult) -> None:
        """Amber hint when the run was captured without a git commit.

        Fires when neither a branch nor a commit was captured — the
        signature of running outside a git repository (or in a repo with
        no commit yet). Lineage is still captured locally, but it isn't
        anchored to a code version, so it can't be ``roar register``-ed for
        reproducible sharing. Nudges the user into a repo if they're
        iterating on code. Same gating as ``next_steps_hint`` (quiet +
        ``hints.enabled``).
        """
        if self._quiet:
            return
        if result.git_short_commit or result.git_branch:
            return
        try:
            from ..integrations.config import config_get

            if config_get("hints.enabled") is False:
                return
        except Exception:
            pass
        c = self._caps.can_color
        self._print(
            style(
                "hint: no git commit captured — this run isn't tied to a code version.",
                "warn_amber",
                enabled=c,
            )
        )
        self._print(
            style(
                "hint: run in a clean git repo to attach a commit to",
                "warn_amber",
                enabled=c,
            )
        )
        self._print(
            style(
                "hint:   your lineage and make it reproducible.",
                "warn_amber",
                enabled=c,
            )
        )

    def tmp_filtered_hint(self, result: RunResult) -> None:
        """Amber hint when /tmp artifact I/O was dropped by the tmp filter.

        Fires only when at least one /tmp path was filtered out of lineage
        (``filters.ignore_tmp_files``, on by default), so artifacts that
        silently landed under /tmp — a common surprise when a workspace lives
        in /tmp — get surfaced. Reuses the count the filter already computed
        (no extra recording). Same gating as ``next_steps_hint`` (quiet +
        ``hints.enabled``).
        """
        if self._quiet:
            return
        n = int((result.filter_counts or {}).get("tmp_files", 0) or 0)
        if n <= 0:
            return
        try:
            from ..integrations.config import config_get

            if config_get("hints.enabled") is False:
                return
        except Exception:
            pass
        c = self._caps.can_color
        plural = "s" if n != 1 else ""
        self._print(
            style(
                f"hint: {n} file{plural} under /tmp ignored (filters.ignore_tmp_files).",
                "warn_amber",
                enabled=c,
            )
        )
        self._print(
            style(
                "hint:   to track them: roar config set filters.ignore_tmp_files false",
                "warn_amber",
                enabled=c,
            )
        )

    # ---- backward-compat one-shot ----------------------------------------

    def show_report(self, result: RunResult, command: list[str], quiet: bool = False) -> None:
        if quiet or self._quiet:
            return
        if self._caps.pipe_mode and not self._show_io_lists:
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
        self.next_steps_hint(result)
        self.tmp_filtered_hint(result)
        self.no_repo_hint(result)

    # ---- stale warnings (unchanged) --------------------------------------

    def show_stale_warnings(
        self,
        stale_upstream: list[int],
        stale_downstream: list[int],
        is_build: bool = False,
    ) -> None:
        if not (stale_upstream or stale_downstream) or self._out is None:
            return
        # `-q` on `roar run` strips all roar-emitted output so the
        # wrapped command's stdout/stderr stand alone. Stale warnings
        # are correctness signals but the user has asked for silence;
        # they can drop `-q` to see them.
        if self._quiet:
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
        lbl = style(f"{label:<{_DETAIL_LABEL_WIDTH}}", "dim", enabled=c)
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
            io_parts.append(_plural(n_in, "input"))
        if n_out:
            io_parts.append(_plural(n_out, "output"))
        if io_parts:
            self._detail("i/o", f" {self._dim_sep()}".join(io_parts))

        # job line — bold hash, no hue, with the step alias when known.
        job_hash = style(result.job_uid, "bold", enabled=c)
        if result.step_number is not None:
            alias = style(f"(alias @{result.step_number})", "dim", enabled=c)
            job_hash = f"{job_hash} {alias}"
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

        # Filter-counts line ("flt"). Only shown if counts were collected
        # for this run (older runs / short-circuit paths leave it empty)
        # and at least one file was filtered.
        self._render_filter_counts(result)

        # Verbose/debug lists.
        if self._show_io_lists:
            self._render_io_lists(result)
        if self._show_dropped_lists:
            self._render_dropped_lists(result)
        # The "next: …" suggestion used to live here, embedded in the
        # summary block. It now fires from `next_steps_hint()` after
        # `done()` so it reads as the hint it is — and so it sits at
        # the bottom of the run output where the user's eye lands last.

    # ---- verbosity-driven blocks ----------------------------------------

    def _render_filter_counts(self, result: RunResult) -> None:
        counts = result.filter_counts or {}
        if not counts:
            return
        parts = []
        for key, label in _FILTER_LABELS:
            if key == "roar_internal":
                continue  # roar's own bookkeeping; not user-meaningful here
            n = int(counts.get(key, 0) or 0)
            if n:
                parts.append(f"{n} {label}")
        if not parts:
            return
        self._detail("ignored i/o", f" {self._dim_sep()}".join(parts))

    def _render_io_lists(self, result: RunResult) -> None:
        c = self._caps.can_color
        if result.inputs:
            self._detail_blank()
            self._detail("in", style(f"{len(result.inputs)} read", "dim", enabled=c))
            for entry in result.inputs:
                path = entry.get("path") if isinstance(entry, dict) else None
                if path:
                    self._print(f"    {path}")
        if result.outputs:
            self._detail_blank()
            self._detail("out", style(f"{len(result.outputs)} written", "dim", enabled=c))
            for entry in result.outputs:
                path = entry.get("path") if isinstance(entry, dict) else None
                if path:
                    self._print(f"    {path}")

    def _render_dropped_lists(self, result: RunResult) -> None:
        dropped = result.dropped_paths or {}
        if not dropped:
            return
        c = self._caps.can_color
        for key, label in _FILTER_LABELS:
            paths = dropped.get(key, [])
            if not paths:
                continue
            self._detail_blank()
            self._detail("ignored i/o", style(f"{len(paths)} {label} (filtered)", "dim", enabled=c))
            for p in paths:
                self._print(f"    {p}")
