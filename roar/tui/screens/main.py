"""Main TUI screen: full-width DAG tree on top, collapsible detail pane below."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Header,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from .. import data as tui_data

REFRESH_INTERVAL_SECONDS = 5.0
TOC_MIN_WIDTH = 90  # below this, hide the sticky TOC and use the full pane width

_STATE_STYLE = {
    "active": "cyan",
    "cached": "green",
    "stale": "yellow",
    "superseded": "dim",
}

_ARTIFACT_STATE_STYLE = {
    "active": "green",
    "stale": "yellow",
    "superseded": "dim",
    "orphaned": "red",
}


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


class _DetailWithToc(Horizontal):
    """Base for detail views: sticky TOC on the left + scrollable body on the right.

    Subclasses define `SECTIONS` as ((key, title, jump_key), ...) and a
    `body_id` for unique element ids.
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


class MainScreen(Screen):
    """Home screen: full-width DAG tree, collapsible detail pane below."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "toggle_artifacts", "Artifacts"),
        Binding("e", "toggle_expanded", "Expanded"),
        Binding("l", "app.push_log", "Log"),
        Binding("slash", "app.open_search", "Search"),
        Binding("exclamation_mark", "app.open_launcher", "Run"),
        Binding("q", "app.quit", "Quit"),
        Binding("tab", "toggle_focus", "Focus tree/detail", show=False, priority=True),
        Binding("escape", "close_detail", "Close detail", show=False),
    ]

    DEFAULT_CSS = """
    #session-info { padding: 0 1; background: $boost; color: $text; }
    Tree { padding: 0 1; }
    #dag-pane { height: 1fr; }
    #detail-pane { display: none; }
    #detail-pane.-visible { display: block; height: 1fr; border-top: solid $primary; }
    """

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self.show_artifacts = True
        self.expanded = False
        self._reloading = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="dag-pane"):
            yield Static("", id="session-info")
            yield Tree("DAG", id="dag-tree")
        with Vertical(id="detail-pane"):
            yield ContentSwitcher(
                Static("", id="empty-detail"),
                JobDetail(id="job-detail"),
                ArtifactDetail(id="artifact-detail"),
                initial="empty-detail",
                id="detail-switcher",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._reload_dag()
        tree = self.query_one("#dag-tree", Tree)
        # Enter must reveal the detail pane without collapsing the step's artifact children.
        tree.auto_expand = False
        tree.focus()
        self.set_interval(REFRESH_INTERVAL_SECONDS, self._reload_dag)

    # --- actions --------------------------------------------------------------

    def action_close_detail(self) -> None:
        pane = self.query_one("#detail-pane")
        if pane.has_class("-visible"):
            pane.remove_class("-visible")
            self.query_one("#dag-tree", Tree).focus()

    def action_toggle_focus(self) -> None:
        if not self.query_one("#detail-pane").has_class("-visible"):
            return
        tree = self.query_one("#dag-tree", Tree)
        if tree.has_focus:
            self._focus_detail()
        else:
            tree.focus()

    def _focus_detail(self) -> None:
        switcher = self.query_one("#detail-switcher", ContentSwitcher)
        if switcher.current == "artifact-detail":
            self.query_one("#artifact-detail", ArtifactDetail).focus_body()
        elif switcher.current == "job-detail":
            self.query_one("#job-detail", JobDetail).focus_body()

    def action_toggle_artifacts(self) -> None:
        self.show_artifacts = not self.show_artifacts
        self._reload_dag()

    def action_toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._reload_dag()

    # --- external nav hooks (used by search modal) ----------------------------

    def focus_target(self, ref: str) -> None:
        """Move the DAG cursor/detail to the entity referenced by `ref` (`@N`, path, or hash)."""
        tree = self.query_one("#dag-tree", Tree)
        for node in _iter_tree_nodes(tree.root):
            data = node.data
            if not data:
                continue
            if data.get("kind") == "step" and ref.startswith("@") and _step_ref(data) == ref:
                tree.select_node(node)
                tree.scroll_to_node(node)
                return
            if data.get("kind") == "artifact" and (
                ref == data.get("path") or (data.get("hash") and ref.startswith(data["hash"][:8]))
            ):
                tree.select_node(node)
                tree.scroll_to_node(node)
                return
        # Fallback: show directly without changing tree cursor
        self._show_by_ref(ref)

    # --- internals ------------------------------------------------------------

    def _reload_dag(self) -> None:
        info = self.query_one("#session-info", Static)
        tree = self.query_one("#dag-tree", Tree)
        saved_cursor = (
            dict(tree.cursor_node.data)
            if tree.cursor_node is not None and tree.cursor_node.data
            else None
        )
        try:
            session = tui_data.load_active_session(self.roar_dir)
            dag = tui_data.load_dag(
                self.roar_dir,
                expanded=self.expanded,
                show_artifacts=self.show_artifacts,
            )
        except ValueError as exc:
            info.update(f"[red]{exc}[/red]")
            tree.clear()
            return

        if session is None:
            info.update("[yellow]No active session[/yellow]")
        else:
            toggles = []
            if self.expanded:
                toggles.append("expanded")
            if self.show_artifacts:
                toggles.append("artifacts")
            toggle_str = f"  [{' '.join(toggles)}]" if toggles else ""
            info.update(
                f"session [magenta]{session.hash[:12]}[/magenta]  "
                f"{dag.total_steps} steps, {dag.stale_count} stale"
                f"{toggle_str}"
            )

        self._reloading = True
        try:
            tree.clear()
            tree.show_root = False
            artifacts_by_producer: dict[int, list] = {}
            for a in dag.artifacts:
                if a.producer_step is not None:
                    artifacts_by_producer.setdefault(int(a.producer_step), []).append(a)

            for node in dag.nodes:
                label = _step_label(node)
                tree_node = tree.root.add(
                    label,
                    data={
                        "kind": "step",
                        "step_number": node.step_number,
                        "job_type": "build" if node.is_build else "run",
                        "state": node.state if isinstance(node.state, str) else node.state.value,
                    },
                    expand=self.show_artifacts,
                )
                if self.show_artifacts:
                    for artifact in artifacts_by_producer.get(int(node.step_number), []):
                        tree_node.add_leaf(
                            _artifact_label(artifact),
                            data={
                                "kind": "artifact",
                                "path": artifact.path,
                                "hash": artifact.hash,
                            },
                        )

            tree.root.expand_all()
        finally:
            self._reloading = False

        # `move_cursor` depends on TreeNode._line which is only assigned after
        # the tree renders, so defer restoration until the next refresh tick.
        self.call_after_refresh(self._restore_cursor, tree, saved_cursor)

        # If detail pane is open, refresh its content too.
        if self.query_one("#detail-pane").has_class("-visible"):
            cursor = tree.cursor_node
            if cursor is not None and cursor.data:
                self._populate_detail(cursor.data)

    def _restore_cursor(self, tree: Tree, saved: dict | None) -> None:
        # `move_cursor` (vs. `select_node`) shifts the cursor without firing
        # NodeSelected — important so reload doesn't auto-open the detail pane.
        if saved is not None:
            for node in _iter_tree_nodes(tree.root):
                if node.data and _cursor_matches(node.data, saved):
                    tree.move_cursor(node)
                    tree.scroll_to_node(node)
                    return
        # No saved cursor (first load) — land on the first step so Enter works immediately.
        if tree.root.children:
            tree.move_cursor(tree.root.children[0])

    def _show_by_ref(self, ref: str) -> None:
        if ref.startswith("@"):
            job = tui_data.load_job(self.roar_dir, self.cwd, ref)
            if job:
                self._populate_detail({"kind": "step", "step_number": int(ref.lstrip("@B"))})
                return
        artifact = tui_data.load_artifact_by_path(self.roar_dir, self.cwd, ref)
        if artifact is None and ref and not ref.startswith("/") and "/" not in ref:
            artifact = tui_data.load_artifact_by_hash(self.roar_dir, self.cwd, ref)
        if artifact:
            self.query_one("#artifact-detail", ArtifactDetail).update_artifact(artifact)
            self.query_one("#detail-switcher", ContentSwitcher).current = "artifact-detail"
            self._reveal_detail()

    def _populate_detail(self, data: dict) -> None:
        switcher = self.query_one("#detail-switcher", ContentSwitcher)
        if data.get("kind") == "step":
            ref = _step_ref(data)
            job = tui_data.load_job(self.roar_dir, self.cwd, ref)
            if job is None:
                switcher.current = "empty-detail"
                return
            self.query_one("#job-detail", JobDetail).update_job(job)
            switcher.current = "job-detail"
        elif data.get("kind") == "artifact":
            path = data.get("path")
            ahash = data.get("hash")
            artifact = None
            if path:
                artifact = tui_data.load_artifact_by_path(self.roar_dir, self.cwd, path)
            if artifact is None and ahash:
                artifact = tui_data.load_artifact_by_hash(self.roar_dir, self.cwd, ahash)
            if artifact is None:
                switcher.current = "empty-detail"
                return
            self.query_one("#artifact-detail", ArtifactDetail).update_artifact(artifact)
            switcher.current = "artifact-detail"

    def _reveal_detail(self) -> None:
        pane = self.query_one("#detail-pane")
        if not pane.has_class("-visible"):
            pane.add_class("-visible")

    # --- tree events ----------------------------------------------------------

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if self._reloading:
            return
        data = event.node.data
        if not data:
            return
        self._populate_detail(data)
        self._reveal_detail()
        self._focus_detail()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if self._reloading:
            return
        if not self.query_one("#detail-pane").has_class("-visible"):
            return
        data = event.node.data
        if data:
            self._populate_detail(data)


def _step_ref(data: dict) -> str:
    prefix = "@B" if data.get("job_type") == "build" else "@"
    return f"{prefix}{data['step_number']}"


def _step_label(node) -> Text:
    state = node.state if isinstance(node.state, str) else node.state.value
    style = _STATE_STYLE.get(state, "")
    ref = f"@B{node.step_number}" if node.is_build else f"@{node.step_number}"
    t = Text()
    t.append(f"{ref}  ", style="bold")
    t.append(f"[{state}] ", style=style)
    cmd = (node.command or "").strip()
    if len(cmd) > 80:
        cmd = cmd[:77] + "…"
    t.append(cmd)
    if node.exit_code not in (None, 0):
        t.append(f"  ✗{node.exit_code}", style="red")
    return t


def _artifact_label(artifact) -> Text:
    state = artifact.state if isinstance(artifact.state, str) else artifact.state.value
    style = _ARTIFACT_STATE_STYLE.get(state, "")
    t = Text()
    t.append("⬇ ", style="dim")
    path = artifact.path or "-"
    if len(path) > 60:
        path = "…" + path[-59:]
    t.append(path)
    t.append(f"  [{state}]", style=style)
    if artifact.hash:
        t.append(f"  {artifact.hash[:8]}", style="magenta")
    return t


def _iter_tree_nodes(node: TreeNode):
    yield node
    for child in node.children:
        yield from _iter_tree_nodes(child)


def _cursor_matches(current: dict, saved: dict) -> bool:
    kind = current.get("kind")
    if kind != saved.get("kind"):
        return False
    if kind == "step":
        return (
            current.get("step_number") == saved.get("step_number")
            and current.get("job_type") == saved.get("job_type")
        )
    if kind == "artifact":
        if saved.get("hash") and current.get("hash") == saved["hash"]:
            return True
        return bool(saved.get("path")) and current.get("path") == saved["path"]
    return False
