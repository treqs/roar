"""Run report presenter — v7 section-based layout.

Renders the lifecycle of ``roar run`` as status lines followed by five
distinct sections (Inputs, Job, Outputs, DAG, Inspect).  All output
goes to stderr so the user command's stdout remains clean for piping.

Color tokens (via ``terminal.style``):
  status_green   -status lines, section headers (bold)
  command_blue   -actionable commands in Inspect block
  dim            -column headers, metadata labels, source-job hashes,
                   counts in parentheses, comments, timing breakdown
  bold           -current job hash (emphasis, no hue)

Hashes carry NO hue.  Weight alone distinguishes them:
  bold    -the current job hash (once, in the Job block)
  regular -artifact hashes in Inputs / Outputs
  dim     -source-job hashes (context)
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

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_PATH_W = 20  # artifact-name column width
_HASH_W = 8  # fixed 8-char digest
_COL_GAP = 4  # spaces between hash columns
_INDENT = "  "  # section-header indent (2 spaces)
_ROW_INDENT = "    "  # data-row indent (4 spaces)
_MAX_ROWS = 5  # max visible rows per section before "… and N more"


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


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def _basename(path: str) -> str:
    try:
        rel = os.path.relpath(path)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return os.path.basename(path) or path


def _digest8(file_info: dict) -> str:
    hashes = file_info.get("hashes", [])
    if not hashes:
        return ""
    return hashes[0].get("digest", "")[:_HASH_W]


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


class _NullProgress:
    """No-op stand-in returned by ``hashing()`` when in pipe/quiet mode."""

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
    """Formats and displays the ``roar run`` lifecycle and summary."""

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
        label = style("tracing", "bold", "status_green", enabled=c)
        params = style(
            f"tracer:{backend or 'auto'}  proxy:{'on' if proxy_active else 'off'}  sync:off",
            "dim",
            enabled=c,
        )
        self._emit_status(label, params)

    def trace_ended(self, duration: float, exit_code: int, backend: str | None = None) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        backend_suffix = ""
        if backend:
            backend_suffix = style(f" [{backend}]", "dim", enabled=c)
        label = style("trace done", "bold", "status_green", enabled=c) + backend_suffix
        exit_s = f"exit {exit_code}"
        if exit_code != 0:
            exit_s = style(exit_s, "red", "bold", enabled=c)
        else:
            exit_s = style(exit_s, "status_green", enabled=c)
        dur_s = style(f"· {self._fmt_dur(duration)}", "dim", enabled=c)
        self._emit_status(label, f"{exit_s} {dur_s}")

    @contextmanager
    def hashing(self, total: int | None = None):
        if self._quiet or self._caps.pipe_mode:
            yield _NullProgress()
            return
        c = self._caps.can_color
        prefix = self._emoji("🦖") + " "
        label = style("hashing", "bold", "status_green", enabled=c)
        if total:
            label += style(f" ({_plural(total, 'artifact')})", "dim", enabled=c)
        frames = CLOCK_FRAMES if self._caps.can_emoji else BRAILLE_FRAMES
        with Spinner(label, prefix=prefix, frames=frames, interval=0.1) as sp:
            yield sp

    def hashed(self, n_artifacts: int, total_bytes: int, duration: float) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        label = style(
            f"hashed {_plural(n_artifacts, 'artifact')}",
            "bold",
            "status_green",
            enabled=c,
        )
        if duration > 0 and total_bytes > 0:
            mbps = (total_bytes / 1024 / 1024) / duration
            tp = style(f"{mbps:.1f} MB/s", "dim", enabled=c)
        else:
            tp = ""
        self._emit_status(label, tp, emoji="🫆")

    def lineage_captured(self) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        c = self._caps.can_color
        label = style("lineage captured", "bold", "status_green", enabled=c)
        self._emit_status(label, emoji="🧬")

    def summary(self, result: RunResult, command: list[str]) -> None:
        if self._quiet or self._caps.pipe_mode:
            return
        self._render_summary(result)

    def done(
        self,
        *,
        exit_code: int,
        trace_duration: float,
        post_duration: float,
    ) -> None:
        if self._quiet:
            return
        verb = "done" if exit_code == 0 else "failed"
        if self._caps.pipe_mode:
            total = trace_duration + post_duration
            line = (
                f"roar: {verb} · {self._fmt_dur(total)} "
                f"(trace {self._fmt_dur(trace_duration)} + "
                f"post {self._fmt_dur(post_duration)}, exit {exit_code})"
            )
            self._print(line)
            return
        c = self._caps.can_color
        label = style(verb, "bold", "status_green" if exit_code == 0 else "red", enabled=c)
        breakdown = style(
            f"(trace {self._fmt_dur(trace_duration)} + post {self._fmt_dur(post_duration)})",
            "dim",
            enabled=c,
        )
        self._emit_status(label, breakdown)

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

    # ---- internal: output primitives -------------------------------------

    def _emoji(self, char: str) -> str:
        return char if self._caps.can_emoji else "roar:"

    def _print(self, line: str = "") -> None:
        print(line, file=self._stream, flush=True)

    def _emit_status(self, label: str, value: str = "", *, emoji: str = "🦖") -> None:
        """Emit a two-column status line: ``🦖 label          value``."""
        prefix = self._emoji(emoji)
        # Pad label to a fixed width so values align in a column.
        padded_label = _pad(f"{prefix} {label}", 28 + _visible_len(prefix))
        self._print(f"{padded_label}{value}")

    def _section_header(self, title: str, col_headers: str = "") -> None:
        c = self._caps.can_color
        styled = style(title, "bold", "status_green", enabled=c)
        self._print(f"{_INDENT}{styled}{col_headers}")

    def _section_row(self, text: str) -> None:
        self._print(f"{_ROW_INDENT}{text}")

    def _fmt_dur(self, seconds: float) -> str:
        if seconds < 0.1:
            return f"{max(1, round(seconds * 1000))}ms"
        return f"{seconds:.1f}s"

    # ---- internal: summary block -----------------------------------------

    def _render_summary(self, result: RunResult) -> None:
        c = self._caps.can_color

        inputs = result.inputs
        outputs = result.outputs
        n_in = len(inputs)
        n_out = len(outputs)

        self._print()

        # -- Inputs section --
        hash_hdr = _pad(style("Hash", "dim", enabled=c), _HASH_W + _COL_GAP)
        src_hdr = style("Source Job", "dim", enabled=c)
        count_dim = style(f" ({n_in})", "dim", enabled=c)
        self._section_header(f"Inputs{count_dim}", f"{'':>{_PATH_W - 6}}{hash_hdr}{src_hdr}")

        shown_in = min(n_in, _MAX_ROWS - 1) if n_in > _MAX_ROWS else n_in
        for i in range(shown_in):
            inp = inputs[i]
            name = _pad(_truncate(_basename(inp["path"]), _PATH_W), _PATH_W)
            digest = _pad(_digest8(inp), _HASH_W + _COL_GAP)
            parent_uid = inp.get("parent_job_uid")
            src = (
                style(str(parent_uid)[:_HASH_W], "dim", enabled=c)
                if parent_uid
                else style("--", "dim", enabled=c)
            )
            self._section_row(f"{name}{digest}{src}")
        if n_in > shown_in:
            self._section_row(style(f"… and {n_in - shown_in} more", "dim", "italic", enabled=c))

        self._print()

        # -- Job section --
        self._section_header("Job")
        job_id = style(result.job_uid, "bold", enabled=c)
        id_label = style("id ", "dim", enabled=c)
        self._section_row(f"{id_label}  {job_id}")
        if result.git_branch or result.git_short_commit:
            branch = result.git_branch or "?"
            commit = result.git_short_commit or "?"
            clean_s = "clean" if result.git_clean else "dirty"
            if result.git_clean:
                clean_s = style(clean_s, "status_green", enabled=c)
            else:
                clean_s = style(clean_s, "red", enabled=c)
            git_label = style("git", "dim", enabled=c)
            self._section_row(f"{git_label}  {branch} @ {commit} {clean_s}")
        env_parts = []
        if result.pip_count:
            env_parts.append(_plural(result.pip_count, "pip"))
        if result.dpkg_count:
            env_parts.append(_plural(result.dpkg_count, "dpkg"))
        if result.env_count:
            env_parts.append(_plural(result.env_count, "var"))
        if env_parts:
            env_label = style("env", "dim", enabled=c)
            env_val = style(" · ".join(env_parts), "dim", enabled=c)
            self._section_row(f"{env_label}  {env_val}")

        self._print()

        # -- Outputs section --
        out_hash_hdr = style("Hash", "dim", enabled=c)
        count_dim = style(f" ({n_out})", "dim", enabled=c)
        self._section_header(f"Outputs{count_dim}", f"{'':>{_PATH_W - 7}}{out_hash_hdr}")

        shown_out = min(n_out, _MAX_ROWS - 1) if n_out > _MAX_ROWS else n_out
        for i in range(shown_out):
            out = outputs[i]
            name = _pad(_truncate(_basename(out["path"]), _PATH_W), _PATH_W)
            digest = _digest8(out)
            self._section_row(f"{name}{digest}")
        if n_out > shown_out:
            self._section_row(style(f"… and {n_out - shown_out} more", "dim", "italic", enabled=c))

        self._print()

        # -- DAG section --
        if result.dag_jobs or result.dag_artifacts:
            self._section_header("DAG")
            dag_parts = []
            if result.dag_jobs:
                dag_parts.append(_plural(result.dag_jobs, "job"))
            if result.dag_artifacts:
                dag_parts.append(_plural(result.dag_artifacts, "artifact"))
            if result.dag_depth:
                dag_parts.append(f"depth {result.dag_depth}")
            dag_val = style(" · ".join(dag_parts), "dim", enabled=c)
            self._section_row(dag_val)
            self._print()

        # -- Inspect section --
        self._section_header("Inspect")
        show_cmd = style(f"roar show --job {result.job_uid}", "command_blue", enabled=c)
        show_comment = style("    # details", "dim", enabled=c)
        self._section_row(f"{show_cmd}{show_comment}")
        if result.interrupted and result.outputs:
            pop_cmd = style("roar pop", "command_blue", enabled=c)
            pop_comment = style("    # undo interrupted run", "dim", enabled=c)
            self._section_row(f"{pop_cmd}{pop_comment}")
        else:
            dag_cmd = style("roar dag", "command_blue", enabled=c)
            dag_comment = style("    # full lineage", "dim", enabled=c)
            self._section_row(f"{dag_cmd}{dag_comment}")

        self._print()
