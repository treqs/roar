"""Detail-pane widgets: TOC sidebar + scrollable section body for jobs/artifacts.

These widgets are decoupled from any host screen so they can drop into a future
diff viewer (per the redesign doc's architectural rules).
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

TOC_MIN_WIDTH = 90  # below this, hide the sticky TOC and use the full pane width


# --- formatting helpers ------------------------------------------------------


def _fmt_ts(ts: float | int | None) -> str:
    if ts is None or ts == 0:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if s < 1:
        return f"{int(s * 1000)}ms"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


def _fmt_size(size: int | None) -> str:
    if not size:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size = size / 1024  # type: ignore[assignment]
    return f"{size:.1f} PiB"


def _short_hash(h: str | None, n: int = 8) -> str:
    if not h:
        return "-"
    return h[:n]


# --- base widget -------------------------------------------------------------


class _DetailWithToc(Horizontal):
    """Base for detail views: sticky TOC on the left + scrollable body on the right.

    Subclasses define `SECTIONS` as ``((section_id, title, jump_key), ...)`` and a
    `body_id` / `toc_id` for unique element ids.
    """

    SECTIONS: tuple[tuple[str, str, str], ...] = ()
    body_id: str = "detail-body"
    toc_id: str = "detail-toc"

    DEFAULT_CSS = """
    _DetailWithToc { height: 1fr; }
    _DetailWithToc .toc {
        width: 13;
        padding-top: 1;
        background: $boost;
    }
    _DetailWithToc .toc.-hidden { display: none; }
    _DetailWithToc .toc Static { color: $text-muted; }
    _DetailWithToc .toc Static.-active {
        color: $accent;
        text-style: bold;
    }
    _DetailWithToc .body { padding: 1 2; }
    _DetailWithToc .body Static.section { margin-bottom: 1; }
    _DetailWithToc .body Static.section-heading {
        color: $accent;
        text-style: bold;
    }
    """

    @staticmethod
    def _toc_label(active: bool, jump_key: str, title: str) -> str:
        marker = "▸" if active else " "
        return f"{marker} {jump_key} {title}"

    def compose(self) -> ComposeResult:
        with Vertical(id=self.toc_id, classes="toc"):
            for key, title, jump_key in self.SECTIONS:
                yield Static(self._toc_label(False, jump_key, title), id=f"toc-{key}")
        with VerticalScroll(id=self.body_id, classes="body"):
            for key, title, _ in self.SECTIONS:
                yield Static(
                    f"── {title} ──", classes="section-heading", id=f"head-{key}"
                )
                yield Static("", classes="section", id=f"sec-{key}")

    def on_mount(self) -> None:
        body = self.query_one(f"#{self.body_id}", VerticalScroll)
        # Track scroll position to highlight the visible section in the TOC.
        self.watch(body, "scroll_y", self._on_body_scroll, init=False)
        self._set_active_section(self.SECTIONS[0][0])

    def on_resize(self, event: events.Resize) -> None:
        toc = self.query_one(f"#{self.toc_id}")
        if event.size.width >= TOC_MIN_WIDTH:
            toc.remove_class("-hidden")
        else:
            toc.add_class("-hidden")

    def on_click(self, event: events.Click) -> None:
        widget = getattr(event, "widget", None)
        if widget is None or not widget.id or not widget.id.startswith("toc-"):
            return
        self.action_jump(widget.id[len("toc-") :])

    def action_jump(self, key: str) -> None:
        body = self.query_one(f"#{self.body_id}", VerticalScroll)
        head = self.query_one(f"#head-{key}", Static)
        body.scroll_to_widget(head, top=True)
        body.focus()
        self._set_active_section(key)

    def focus_body(self) -> None:
        self.query_one(f"#{self.body_id}", VerticalScroll).focus()

    def _on_body_scroll(self, value: float) -> None:
        # Compare virtual positions: a head is "at or above the viewport top" iff
        # its virtual y is at or below the current scroll offset. Avoids reading
        # screen `region.y` which lags one layout pass behind a scroll change.
        threshold = value + 1
        active = self.SECTIONS[0][0]
        for key, _, _ in self.SECTIONS:
            head = self.query_one(f"#head-{key}", Static)
            if head.virtual_region.y <= threshold:
                active = key
            else:
                break
        self._set_active_section(active)

    def _set_active_section(self, key: str) -> None:
        for k, title, jump_key in self.SECTIONS:
            row = self.query_one(f"#toc-{k}", Static)
            is_active = k == key
            row.set_class(is_active, "-active")
            row.update(self._toc_label(is_active, jump_key, title))


# --- job detail --------------------------------------------------------------


class JobDetail(_DetailWithToc):
    """Job detail with TOC: summary / command / inputs / outputs / labels / git / env."""

    body_id = "job-body"
    toc_id = "job-toc"

    SECTIONS = (
        ("summary", "Summary", "s"),
        ("command", "Command", "c"),
        ("inputs", "Inputs", "i"),
        ("outputs", "Outputs", "o"),
        ("labels", "Labels", "l"),
        ("git", "Git", "g"),
        ("env", "Env", "e"),
    )

    BINDINGS = [
        Binding("s", "jump('summary')", "Summary", show=False),
        Binding("c", "jump('command')", "Command", show=False),
        Binding("i", "jump('inputs')", "Inputs", show=False),
        Binding("o", "jump('outputs')", "Outputs", show=False),
        Binding("l", "jump('labels')", "Labels", show=False),
        Binding("g", "jump('git')", "Git", show=False),
        Binding("e", "jump('env')", "Env", show=False),
    ]

    def update_job(self, summary) -> None:
        self.query_one("#sec-summary", Static).update(_render_job_overview(summary))
        self.query_one("#sec-command", Static).update(_render_command(summary))
        self.query_one("#sec-inputs", Static).update(_render_artifact_list(summary.inputs))
        self.query_one("#sec-outputs", Static).update(_render_artifact_list(summary.outputs))
        self.query_one("#sec-labels", Static).update(_render_labels(summary.labels))
        self.query_one("#sec-git", Static).update(_render_git(summary))
        self.query_one("#sec-env", Static).update(_render_env(summary))


def _render_job_overview(summary) -> Text:
    prefix = "@B" if summary.job_type == "build" else "@"
    step = f"{prefix}{summary.step_number}" if summary.step_number is not None else "-"
    status = (
        "OK"
        if summary.exit_code == 0
        else (f"FAIL ({summary.exit_code})" if summary.exit_code is not None else "?")
    )
    status_style = "green" if summary.exit_code == 0 else ("red" if summary.exit_code else "yellow")

    t = Text()
    t.append("Job ", style="bold")
    t.append(summary.job_uid or "-", style="magenta")
    t.append("\n")
    t.append(f"Step:      {step}\n")
    t.append(f"Started:   {_fmt_ts(summary.timestamp)}\n")
    t.append(f"Duration:  {_fmt_duration(summary.duration_seconds)}\n")
    t.append("Status:    ")
    t.append(status + "\n", style=status_style)
    if summary.step_name:
        t.append(f"Name:      {summary.step_name}\n")
    return t


def _render_command(summary) -> Text:
    return Text(summary.command or "(no command)", style="white")


def _render_artifact_list(artifacts) -> Text:
    if not artifacts:
        return Text("(none)", style="dim")
    t = Text()
    for a in artifacts:
        primary = next((h.digest for h in a.hashes if h.algorithm == "blake3"), None)
        t.append("• ", style="cyan")
        t.append(a.path or "-", style="bold")
        t.append(f"  {_fmt_size(a.size)}  ", style="dim")
        t.append(_short_hash(primary) + "\n", style="magenta")
    return t


def _render_labels(labels: dict | None) -> Text:
    if not labels:
        return Text("(no labels)", style="dim")
    t = Text()
    for k, v in sorted(labels.items()):
        t.append(f"{k}", style="cyan")
        t.append(f" = {v}\n")
    return t


def _render_git(summary) -> Text:
    t = Text()
    t.append(f"Commit:  {summary.git_commit or '-'}\n")
    t.append(f"Branch:  {summary.git_branch or '-'}\n")
    return t


def _render_env(summary) -> Text:
    t = Text()
    if not summary.metadata and not summary.telemetry:
        return Text("(no telemetry)", style="dim")
    if summary.metadata:
        t.append("metadata:\n", style="bold")
        for k, v in summary.metadata.items():
            t.append(f"  {k}: {v}\n")
    if summary.telemetry:
        t.append("\ntelemetry:\n", style="bold")
        for k, v in summary.telemetry.items():
            t.append(f"  {k}: {v}\n")
    return t


# --- artifact detail ---------------------------------------------------------


class ArtifactDetail(_DetailWithToc):
    """Artifact detail with TOC: summary / hashes / paths / producers / consumers / labels.

    Producers and consumers borrow `i`/`o` from the job view's inputs/outputs —
    they're the same flow direction (this artifact's input side / output side).
    """

    body_id = "artifact-body"
    toc_id = "artifact-toc"

    SECTIONS = (
        ("summary", "Summary", "s"),
        ("hashes", "Hashes", "h"),
        ("locations", "Paths", "p"),
        ("producers", "Producers", "i"),
        ("consumers", "Consumers", "o"),
        ("labels", "Labels", "l"),
    )

    BINDINGS = [
        Binding("s", "jump('summary')", "Summary", show=False),
        Binding("h", "jump('hashes')", "Hashes", show=False),
        Binding("p", "jump('locations')", "Paths", show=False),
        Binding("i", "jump('producers')", "Producers", show=False),
        Binding("o", "jump('consumers')", "Consumers", show=False),
        Binding("l", "jump('labels')", "Labels", show=False),
    ]

    def update_artifact(self, summary) -> None:
        self.query_one("#sec-summary", Static).update(_render_artifact_overview(summary))
        self.query_one("#sec-hashes", Static).update(_render_artifact_hashes(summary))
        self.query_one("#sec-locations", Static).update(_render_artifact_locations(summary))
        self.query_one("#sec-producers", Static).update(_render_artifact_jobs(summary.produced_by))
        self.query_one("#sec-consumers", Static).update(_render_artifact_jobs(summary.consumed_by))
        self.query_one("#sec-labels", Static).update(_render_labels(summary.labels))


def _render_artifact_overview(summary) -> Text:
    primary = next((h.digest for h in summary.hashes if h.algorithm == "blake3"), None)
    t = Text()
    t.append("Artifact ", style="bold")
    t.append(_short_hash(primary, 12) + "\n", style="magenta")
    t.append(f"Kind:       {summary.kind or '-'}\n")
    t.append(f"Size:       {_fmt_size(summary.size)}\n")
    t.append(f"First seen: {_fmt_ts(summary.first_seen_at)}\n")
    if summary.first_seen_path:
        t.append(f"Path:       {summary.first_seen_path}\n")
    return t


def _render_artifact_hashes(summary) -> Text:
    if not summary.hashes:
        return Text("(none)", style="dim")
    t = Text()
    for h in summary.hashes:
        t.append(f"{h.algorithm}: ", style="cyan")
        t.append(f"{h.digest}\n")
    return t


def _render_artifact_locations(summary) -> Text:
    if not summary.locations:
        return Text("(no recorded paths)", style="dim")
    t = Text()
    for loc in summary.locations:
        t.append(f"{loc.path}\n")
    return t


def _render_artifact_jobs(jobs) -> Text:
    if not jobs:
        return Text("(none)", style="dim")
    t = Text()
    for j in jobs:
        t.append(f"{(j.job_uid or '-')[:8]}", style="magenta")
        t.append(f"  {j.command or ''}\n")
    return t


__all__ = ["JobDetail", "ArtifactDetail", "TOC_MIN_WIDTH"]
