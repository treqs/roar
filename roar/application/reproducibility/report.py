"""Shared reproducibility checklist for `roar register`/`put` and `roar reproduce`.

One definition of "what makes a lineage reproducible," evaluated from whatever
facts each command has, and rendered the same way at both ends — so register's
"here's what you published" and reproduce's "here's what was recorded" always
agree. Warn, never block: an unchecked box is a heads-up, not a gate.

Rendering is a punchlist: every item shows its own ``[✅]``/``[❌]`` with the
detail (info when passed, the exception/fix when failed) indented below — the
same style register uses for the operational steps it performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Re-exported so existing imports (`from ...reproducibility.report import
# is_shareable_remote`) keep working; the single definition lives in utils.
from ...utils.git_url import is_shareable_remote as is_shareable_remote

_PASS = "✅"
_FAIL = "❌"


def _is_ephemeral_tmp(path: str) -> bool:
    """An input under an ephemeral tmp dir — guaranteed gone on another machine.

    Inlined (not imported from the query layer) so this module stays independent
    of the /tmp-handling branch; the predicate is intentionally identical.
    """
    return bool(path) and (path.startswith("/tmp/") or "/private/var/folders/" in path)


@dataclass
class ReproCheck:
    """A punchlist item. ``note`` shows when passed; ``detail`` when failed."""

    key: str
    label: str
    ok: bool
    detail: str = ""  # the exception / how-to-fix, shown when not ok
    note: str = ""  # supporting info, shown when ok
    na: bool = False  # not-applicable in this context (e.g. publish status on a dry run)


@dataclass
class ReproducibilityReport:
    checks: list[ReproCheck] = field(default_factory=list)

    @property
    def applicable(self) -> list[ReproCheck]:
        return [c for c in self.checks if not c.na]

    @property
    def passed(self) -> list[ReproCheck]:
        return [c for c in self.applicable if c.ok]

    @property
    def failed(self) -> list[ReproCheck]:
        return [c for c in self.applicable if not c.ok]

    @property
    def all_ok(self) -> bool:
        return not self.failed


def build_report(
    *,
    committed: bool,
    pushed: bool,
    runtime_ok: bool,
    unsourced_paths: list[str],
    on_glaas: bool,
    single_commit: bool = True,
    notes: dict[str, str] | None = None,
    na: dict[str, str] | None = None,
) -> ReproducibilityReport:
    """Assemble the canonical checklist from already-computed facts.

    ``notes`` attaches supporting info (shown when the check passed) by check
    key — e.g. register passes ``{"committed": "tagged roar/ab12", "on_glaas":
    "2 jobs · 7 artifacts"}`` so the punchlist doubles as its operation receipt.
    """
    report = ReproducibilityReport(
        checks=[
            ReproCheck(
                "committed",
                "code committed to git",
                committed,
                "run outside a git repo — the code isn't versioned, so it can't be restored",
            ),
            ReproCheck(
                "single_commit",
                "single git commit across all steps",
                single_commit,
                "steps span more than one commit — reproduce checks out the last, "
                "so results may differ from the original",
            ),
            ReproCheck(
                "pushed",
                "commit reachable on a remote",
                pushed,
                "no shareable git remote — others can't fetch the exact code "
                "(add one: `git remote add origin <url>`)",
            ),
            ReproCheck(
                "inputs_sourced",
                "all inputs sourced",
                not unsourced_paths,
                _unsourced_detail(unsourced_paths),
            ),
            ReproCheck(
                "runtime",
                "runtime captured (interpreter + packages)",
                runtime_ok,
                "no interpreter/packages recorded for the run",
            ),
            ReproCheck(
                "on_glaas",
                "lineage saved on glaas.ai",
                on_glaas,
                "only on this machine — run `roar register` to publish it",
            ),
        ]
    )
    for check in report.checks:
        if notes and check.key in notes:
            check.note = notes[check.key]
        if na and check.key in na:
            check.na = True
            check.note = na[check.key]
    return report


def runtime_captured(pipeline) -> bool:
    """True if the lineage recorded a runtime (a Python interpreter version)."""
    from ...execution.reproduction.pipeline_metadata import PipelineMetadataParser

    try:
        runtime = PipelineMetadataParser().first_runtime(pipeline.build_steps, pipeline.run_steps)
        return bool((runtime.get("python") or {}).get("version"))
    except Exception:
        return False


def unsourced_input_paths(roar_dir, cwd, ref: str) -> list[str]:
    """Paths of inputs nothing tracked produced, for the given target (best-effort)."""
    from ..query.inputs import build_inputs_summary
    from ..query.requests import InputsQueryRequest

    try:
        summary = build_inputs_summary(
            InputsQueryRequest(roar_dir=roar_dir, cwd=cwd, ref=ref, unsourced=True)
        )
    except Exception:
        return []
    return [a.path or a.artifact_id for a in summary.artifacts]


def _unsourced_detail(paths: list[str]) -> str:
    if not paths:
        return ""
    n = len(paths)
    shown = " · ".join(paths[:3]) + (" · …" if n > 3 else "")
    detail = f"{n} input(s) nothing tracked produced (won't exist elsewhere): {shown}"
    tmp = sum(1 for p in paths if _is_ephemeral_tmp(p))
    if tmp:
        detail += f"; {tmp} in /tmp — those definitely won't survive"
    return detail


def render_punchlist(items: list[ReproCheck], *, title: str) -> str:
    """Render a checkbox punchlist: every item on its own line, detail indented.

    ``[✅] label`` with the ``note`` below when passed; ``[❌] label`` with the
    ``→ detail`` exception below when failed; ``[-] label`` for a not-applicable
    item (e.g. publish status on a dry run). Not-applicable items are excluded
    from the X/Y count. Shared by the reproducibility checklist and register's
    operational summary so they read identically."""
    applicable = [it for it in items if not it.na]
    n_ok = sum(1 for it in applicable if it.ok)
    lines = [f"{title} — {n_ok}/{len(applicable)}"]
    for it in items:
        mark = "-" if it.na else (_PASS if it.ok else _FAIL)
        lines.append(f"  [{mark}] {it.label}")
        if (it.na and it.note) or (it.ok and it.note):
            lines.append(f"       {it.note}")
        elif not it.ok and it.detail:
            lines.append(f"       → {it.detail}")
    return "\n".join(lines)


def render_report(report: ReproducibilityReport, *, title: str = "Reproducibility") -> str:
    """Render the reproducibility punchlist, with one warning if anything failed."""
    out = render_punchlist(report.checks, title=title)
    if not report.all_ok:
        out += "\n⚠  This lineage may not reproduce as recorded — see the unchecked items."
    return out
