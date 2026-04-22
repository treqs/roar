"""Log screen: scrollable table of recent jobs in the active session."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import data as tui_data


def _fmt_ts(ts: float | int | None) -> str:
    if ts is None or ts == 0:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _fmt_duration(s: float | int | None) -> str:
    if s is None:
        return "-"
    try:
        val = float(s)
    except (TypeError, ValueError):
        return "-"
    if val < 1:
        return f"{int(val * 1000)}ms"
    if val < 60:
        return f"{val:.1f}s"
    m, sec = divmod(int(val), 60)
    return f"{m}m{sec:02d}s"


class LogScreen(Screen):
    """Shows `roar log`-style history in an interactive table."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "pop_screen", "Back"),
        Binding("enter", "open_selected", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    #log-header { padding: 0 1; background: $boost; }
    DataTable { height: 1fr; }
    """

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Job log — press Enter to open, Esc to go back", id="log-header")
        yield DataTable(id="log-table", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.add_columns("UID", "STEP", "TIME", "DUR", "STATUS", "COMMAND")
        self._reload()
        table.focus()

    def action_refresh(self) -> None:
        self._reload()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_open_selected(self) -> None:
        table = self.query_one("#log-table", DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            step_ref = table.get_row(row_key)[1]
        except Exception:
            self.app.bell()
            return
        if not step_ref or step_ref == "-":
            self.app.bell()
            return
        self.app.pop_screen()
        # Ask main screen to focus this ref
        if self.app.screen_stack:
            top = self.app.screen_stack[-1]
            focus_target = getattr(top, "focus_target", None)
            if callable(focus_target):
                focus_target(str(step_ref))

    def _reload(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.clear()
        try:
            summary = tui_data.load_log(self.roar_dir)
        except Exception as exc:
            table.add_row("", "", "", "", "ERR", str(exc))
            return

        for job in summary.jobs:
            prefix = "@B" if job.job_type == "build" else "@"
            step = f"{prefix}{job.step_number}" if job.step_number is not None else "-"
            status = (
                "OK"
                if job.exit_code == 0
                else (f"FAIL ({job.exit_code})" if job.exit_code is not None else "?")
            )
            table.add_row(
                (job.job_uid or "-")[:8],
                step,
                _fmt_ts(job.timestamp),
                _fmt_duration(job.duration_seconds),
                status,
                (job.command or "")[:80],
            )
