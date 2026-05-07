"""Detail-pane widgets: TOC sidebar + scrollable section body for jobs/artifacts.

These widgets are decoupled from any host screen so they can drop into a future
diff viewer (per the redesign doc's architectural rules).
"""

from __future__ import annotations

from datetime import datetime

from rich.style import Style
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
    `body_id` / `toc_id` for unique element ids. Subclasses also override
    `_collect_links` to expose clickable spans for keyboard traversal.
    """

    SECTIONS: tuple[tuple[str, str, str], ...] = ()
    body_id: str = "detail-body"
    toc_id: str = "detail-toc"

    BINDINGS = [
        # Up/Down repurpose the body's default line-scroll into TOC section nav
        # (vertical TOC ↔ vertical arrows). PgUp/PgDn keep the body's default
        # paging behavior. priority=True is required because the focused
        # VerticalScroll has its own up/down bindings we need to outrank.
        Binding("up", "section_prev", "Prev section", show=False, priority=True),
        Binding("down", "section_next", "Next section", show=False, priority=True),
        # Left/Right cycle through clickable links in the body.
        Binding("left", "prev_link", "Prev link", show=False, priority=True),
        Binding("right", "next_link", "Next link", show=False, priority=True),
        Binding("enter", "follow_link", "Follow link", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Each link: (kind, target, section_id). `kind` drives which screen
        # action follows it; `section_id` is where to scroll on selection.
        self._links: list[tuple[str, str, str]] = []
        self._link_idx: int = -1

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

    # --- link traversal -------------------------------------------------------

    def action_section_prev(self) -> None:
        self._step_section(-1)

    def action_section_next(self) -> None:
        self._step_section(+1)

    def _step_section(self, delta: int) -> None:
        keys = [k for k, _, _ in self.SECTIONS]
        if not keys:
            return
        current = self._current_active_section_key() or keys[0]
        try:
            idx = keys.index(current)
        except ValueError:
            idx = 0
        new_idx = max(0, min(len(keys) - 1, idx + delta))
        if new_idx != idx:
            self.action_jump(keys[new_idx])

    def _current_active_section_key(self) -> str | None:
        for k, _, _ in self.SECTIONS:
            row = self.query_one(f"#toc-{k}", Static)
            if row.has_class("-active"):
                return k
        return None

    def action_next_link(self) -> None:
        if not self._links:
            return
        self._link_idx = (self._link_idx + 1) % len(self._links)
        self._refresh_link_render()
        self._scroll_to_active_link()

    def action_prev_link(self) -> None:
        if not self._links:
            return
        self._link_idx = (self._link_idx - 1) % len(self._links)
        self._refresh_link_render()
        self._scroll_to_active_link()

    def action_follow_link(self) -> None:
        if not (0 <= self._link_idx < len(self._links)):
            return
        kind, target, _ = self._links[self._link_idx]
        screen = self.screen
        if kind == "job_uid":
            screen.action_open_job_uid(target)  # type: ignore[attr-defined]
        elif kind == "artifact_path":
            screen.action_open_artifact_path(target)  # type: ignore[attr-defined]
        elif kind == "command":
            self.app.action_open_launcher_with_command(target)  # type: ignore[attr-defined]

    def _scroll_to_active_link(self) -> None:
        if not (0 <= self._link_idx < len(self._links)):
            return
        _, _, section_id = self._links[self._link_idx]
        body = self.query_one(f"#{self.body_id}", VerticalScroll)
        head = self.query_one(f"#head-{section_id}", Static)
        body.scroll_to_widget(head, top=True)
        self._set_active_section(section_id)

    def _active_link_target(self) -> str | None:
        if 0 <= self._link_idx < len(self._links):
            return self._links[self._link_idx][1]
        return None

    def _refresh_link_render(self) -> None:  # pragma: no cover - subclass hook
        """Subclass override: re-render link-bearing sections with the current
        active link target highlighted."""

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
        old_target = self._links[self._link_idx] if 0 <= self._link_idx < len(self._links) else None
        self._summary = summary
        links: list[tuple[str, str, str]] = []
        if summary.command:
            links.append(("command", summary.command, "command"))
        links.extend(
            ("artifact_path", a.path, "inputs") for a in summary.inputs if a.path
        )
        links.extend(
            ("artifact_path", a.path, "outputs") for a in summary.outputs if a.path
        )
        self._links = links
        # Preserve highlight across auto-refreshes if the same target still exists.
        self._link_idx = links.index(old_target) if old_target in links else -1
        self._render_sections()

    def _render_sections(self) -> None:
        target = self._active_link_target()
        s = self._summary
        self.query_one("#sec-summary", Static).update(_render_job_overview(s))
        self.query_one("#sec-command", Static).update(_render_command(s, target))
        self.query_one("#sec-inputs", Static).update(
            _render_artifact_list(s.inputs, active_link_target=target)
        )
        self.query_one("#sec-outputs", Static).update(
            _render_artifact_list(s.outputs, active_link_target=target)
        )
        self.query_one("#sec-labels", Static).update(_render_labels(s.labels))
        self.query_one("#sec-git", Static).update(_render_git(s))
        self.query_one("#sec-env", Static).update(_render_env(s))

    def _refresh_link_render(self) -> None:
        target = self._active_link_target()
        s = self._summary
        self.query_one("#sec-command", Static).update(_render_command(s, target))
        self.query_one("#sec-inputs", Static).update(
            _render_artifact_list(s.inputs, active_link_target=target)
        )
        self.query_one("#sec-outputs", Static).update(
            _render_artifact_list(s.outputs, active_link_target=target)
        )


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


def _render_command(summary, active_link_target: str | None = None) -> Text:
    cmd = summary.command
    if not cmd:
        return Text("(no command)", style="dim")
    is_active = cmd == active_link_target
    t = Text()
    t.append(
        cmd,
        style=Style(
            color="white",
            underline=True,
            reverse=is_active,
            meta={"@click": f"app.open_launcher_with_command({cmd!r})"},
        ),
    )
    t.append("\n\n")
    t.append("Enter / click → relaunch via tmux", style="dim")
    return t


def _render_artifact_list(artifacts, active_link_target: str | None = None) -> Text:
    if not artifacts:
        return Text("(none)", style="dim")
    t = Text()
    for a in artifacts:
        primary = next((h.digest for h in a.hashes if h.algorithm == "blake3"), None)
        t.append("• ", style="cyan")
        path_str = a.path or "-"
        if a.path:
            is_active = a.path == active_link_target
            t.append(
                path_str,
                style=Style(
                    bold=True,
                    underline=True,
                    reverse=is_active,
                    meta={"@click": f"screen.open_artifact_path({path_str!r})"},
                ),
            )
        else:
            t.append(path_str, style="bold")
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

    `i`/`o` map by the role this artifact plays in those jobs:
    a producer's `o`utput is this artifact, a consumer takes it as `i`nput.
    """

    body_id = "artifact-body"
    toc_id = "artifact-toc"

    SECTIONS = (
        ("summary", "Summary", "s"),
        ("hashes", "Hashes", "h"),
        ("locations", "Paths", "p"),
        ("producers", "Producers", "o"),
        ("consumers", "Consumers", "i"),
        ("labels", "Labels", "l"),
    )

    BINDINGS = [
        Binding("s", "jump('summary')", "Summary", show=False),
        Binding("h", "jump('hashes')", "Hashes", show=False),
        Binding("p", "jump('locations')", "Paths", show=False),
        Binding("o", "jump('producers')", "Producers", show=False),
        Binding("i", "jump('consumers')", "Consumers", show=False),
        Binding("l", "jump('labels')", "Labels", show=False),
    ]

    def update_artifact(self, summary, current_session_hash: str | None = None) -> None:
        old_target = self._links[self._link_idx] if 0 <= self._link_idx < len(self._links) else None
        self._summary = summary
        self._current_session_hash = current_session_hash
        self._links = [
            ("job_uid", j.job_uid, "producers") for j in summary.produced_by if j.job_uid
        ] + [
            ("job_uid", j.job_uid, "consumers") for j in summary.consumed_by if j.job_uid
        ]
        self._link_idx = self._links.index(old_target) if old_target in self._links else -1
        self._render_sections()

    def _render_sections(self) -> None:
        target = self._active_link_target()
        s = self._summary
        self.query_one("#sec-summary", Static).update(_render_artifact_overview(s))
        self.query_one("#sec-hashes", Static).update(_render_artifact_hashes(s))
        self.query_one("#sec-locations", Static).update(_render_artifact_locations(s))
        self.query_one("#sec-producers", Static).update(
            _render_artifact_jobs(
                s.produced_by, self._current_session_hash, active_link_target=target
            )
        )
        self.query_one("#sec-consumers", Static).update(
            _render_artifact_jobs(
                s.consumed_by, self._current_session_hash, active_link_target=target
            )
        )
        self.query_one("#sec-labels", Static).update(_render_labels(s.labels))

    def _refresh_link_render(self) -> None:
        target = self._active_link_target()
        s = self._summary
        self.query_one("#sec-producers", Static).update(
            _render_artifact_jobs(
                s.produced_by, self._current_session_hash, active_link_target=target
            )
        )
        self.query_one("#sec-consumers", Static).update(
            _render_artifact_jobs(
                s.consumed_by, self._current_session_hash, active_link_target=target
            )
        )


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


def _render_artifact_jobs(
    jobs,
    current_session_hash: str | None = None,
    active_link_target: str | None = None,
) -> Text:
    if not jobs:
        return Text("(none)", style="dim")
    t = Text()
    for j in jobs:
        in_session = (
            current_session_hash is not None
            and j.session_hash is not None
            and j.session_hash == current_session_hash
        )
        marker = "● " if in_session else "  "  # filled dot = "in this session"
        t.append(marker, style="green" if in_session else "dim")
        uid_short = (j.job_uid or "-")[:8]
        if j.job_uid:
            is_active = j.job_uid == active_link_target
            t.append(
                uid_short,
                style=Style(
                    color="magenta",
                    underline=True,
                    reverse=is_active,
                    meta={"@click": f"screen.open_job_uid({j.job_uid!r})"},
                ),
            )
        else:
            t.append(uid_short, style="magenta")
        t.append(f"  {j.command or ''}\n")
    return t


__all__ = ["JobDetail", "ArtifactDetail", "TOC_MIN_WIDTH"]
