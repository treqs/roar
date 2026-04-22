"""Main TUI screen: session DAG on the left, entity detail on the right."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

from .. import data as tui_data

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


class JobDetail(TabbedContent):
    """Tabbed detail view for a job (Overview/Inputs/Outputs/Labels/Git/Env)."""

    DEFAULT_CSS = """
    JobDetail { height: 1fr; }
    JobDetail Static.pane { padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        with TabPane("Overview", id="tab-overview"):
            yield Static("", classes="pane", id="pane-overview")
        with TabPane("Inputs", id="tab-inputs"):
            yield Static("", classes="pane", id="pane-inputs")
        with TabPane("Outputs", id="tab-outputs"):
            yield Static("", classes="pane", id="pane-outputs")
        with TabPane("Labels", id="tab-labels"):
            yield Static("", classes="pane", id="pane-labels")
        with TabPane("Git", id="tab-git"):
            yield Static("", classes="pane", id="pane-git")
        with TabPane("Env", id="tab-env"):
            yield Static("", classes="pane", id="pane-env")

    def update_job(self, summary) -> None:
        self.query_one("#pane-overview", Static).update(_render_job_overview(summary))
        self.query_one("#pane-inputs", Static).update(_render_artifact_list(summary.inputs))
        self.query_one("#pane-outputs", Static).update(_render_artifact_list(summary.outputs))
        self.query_one("#pane-labels", Static).update(_render_labels(summary.labels))
        self.query_one("#pane-git", Static).update(_render_git(summary))
        self.query_one("#pane-env", Static).update(_render_env(summary))


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
    t.append("\nCommand:\n", style="bold")
    t.append("  ")
    t.append(summary.command or "(no command)", style="white")
    return t


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


class ArtifactDetail(Static):
    """Single-pane artifact detail view."""

    DEFAULT_CSS = """
    ArtifactDetail { padding: 1 2; height: 1fr; }
    """

    def update_artifact(self, summary) -> None:
        primary = next((h.digest for h in summary.hashes if h.algorithm == "blake3"), None)
        t = Text()
        t.append("Artifact ", style="bold")
        t.append(_short_hash(primary, 12) + "\n", style="magenta")
        t.append(f"Kind:       {summary.kind or '-'}\n")
        t.append(f"Size:       {_fmt_size(summary.size)}\n")
        t.append(f"First seen: {_fmt_ts(summary.first_seen_at)}\n")
        if summary.first_seen_path:
            t.append(f"Path:       {summary.first_seen_path}\n")
        t.append("\nHashes:\n", style="bold")
        for h in summary.hashes:
            t.append(f"  {h.algorithm}: ", style="cyan")
            t.append(f"{h.digest}\n")
        if summary.locations:
            t.append("\nLocations:\n", style="bold")
            for loc in summary.locations:
                t.append(f"  {loc.path}\n")
        t.append("\nProduced by:\n", style="bold")
        if summary.produced_by:
            for j in summary.produced_by:
                t.append(f"  {(j.job_uid or '-')[:8]}  {j.command or ''}\n")
        else:
            t.append("  (none)\n", style="dim")
        t.append("\nConsumed by:\n", style="bold")
        if summary.consumed_by:
            for j in summary.consumed_by:
                t.append(f"  {(j.job_uid or '-')[:8]}  {j.command or ''}\n")
        else:
            t.append("  (none)\n", style="dim")
        if summary.labels:
            t.append("\nLabels:\n", style="bold")
            for k, v in sorted(summary.labels.items()):
                t.append(f"  {k}", style="cyan")
                t.append(f" = {v}\n")
        self.update(t)


class MainScreen(Screen):
    """Home screen: DAG tree left, entity detail right."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("a", "toggle_artifacts", "Artifacts"),
        Binding("e", "toggle_expanded", "Expanded"),
        Binding("l", "app.push_log", "Log"),
        Binding("slash", "app.open_search", "Search"),
        Binding("exclamation_mark", "app.open_launcher", "Run"),
        Binding("q", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    #left-pane { width: 40%; border-right: solid $primary; }
    #session-info { padding: 0 1; background: $boost; color: $text; }
    #detail-switcher { width: 60%; }
    Tree { padding: 0 1; }
    """

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self.show_artifacts = True
        self.expanded = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Vertical(
                Static("", id="session-info"),
                Tree("DAG", id="dag-tree"),
                id="left-pane",
            ),
            ContentSwitcher(
                Static(
                    "Select a step or artifact on the left to see details.",
                    id="empty-detail",
                ),
                JobDetail(id="job-detail"),
                ArtifactDetail(id="artifact-detail"),
                initial="empty-detail",
                id="detail-switcher",
            ),
            id="main-row",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._reload_dag()
        tree = self.query_one("#dag-tree", Tree)
        tree.focus()

    # --- actions --------------------------------------------------------------

    def action_refresh(self) -> None:
        self._reload_dag()

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

    def _show_by_ref(self, ref: str) -> None:
        switcher = self.query_one("#detail-switcher", ContentSwitcher)
        if ref.startswith("@"):
            job = tui_data.load_job(self.roar_dir, self.cwd, ref)
            if job:
                self.query_one("#job-detail", JobDetail).update_job(job)
                switcher.current = "job-detail"
                return
        artifact = tui_data.load_artifact_by_path(self.roar_dir, self.cwd, ref)
        if artifact is None and ref and not ref.startswith("/") and "/" not in ref:
            artifact = tui_data.load_artifact_by_hash(self.roar_dir, self.cwd, ref)
        if artifact:
            self.query_one("#artifact-detail", ArtifactDetail).update_artifact(artifact)
            switcher.current = "artifact-detail"

    # --- tree events ----------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not data:
            return
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
