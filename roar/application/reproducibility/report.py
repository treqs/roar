"""Shared reproducibility checklist for `roar register`/`put` and `roar reproduce`.

One definition of "what makes a lineage reproducible," evaluated from whatever
facts each command has, and rendered the same way at both ends — so register's
"here's what you published" and reproduce's "here's what was recorded" always
agree. Warn, never block: an unchecked box is a heads-up, not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _is_ephemeral_tmp(path: str) -> bool:
    """An input under an ephemeral tmp dir — guaranteed gone on another machine.

    Inlined (not imported from the query layer) so this module stays independent
    of the /tmp-handling branch; the predicate is intentionally identical.
    """
    return bool(path) and (path.startswith("/tmp/") or "/private/var/folders/" in path)


@dataclass
class ReproCheck:
    """A single checklist item. ``detail`` is shown only when not ``ok``."""

    key: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class ReproducibilityReport:
    checks: list[ReproCheck] = field(default_factory=list)

    @property
    def passed(self) -> list[ReproCheck]:
        return [c for c in self.checks if c.ok]

    @property
    def failed(self) -> list[ReproCheck]:
        return [c for c in self.checks if not c.ok]

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
) -> ReproducibilityReport:
    """Assemble the canonical checklist from already-computed facts."""
    return ReproducibilityReport(
        checks=[
            ReproCheck(
                "committed",
                "code committed to git",
                committed,
                "run outside a git repo — the code isn't versioned, so it can't be restored",
            ),
            ReproCheck(
                "pushed",
                "commit reachable on a remote",
                pushed,
                "no git remote recorded — others can't fetch the exact code",
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


def render_report(
    report: ReproducibilityReport,
    *,
    title: str = "Reproducibility",
    links: list[tuple[str, str]] | None = None,
) -> str:
    """Render the checklist: collapse the green boxes, expand only the failed ones.

    All green -> a single confirming line. Any failing -> the failed boxes with
    their detail, the passed ones folded into one line, and a single warning.
    """
    lines: list[str] = []
    total = len(report.checks)

    if report.all_ok:
        lines.append(f"{title}: ✓ all {total} checks passed")
    else:
        lines.append(f"{title}: {len(report.passed)}/{total} checks passed")
        for check in report.failed:
            lines.append(f"  [ ] {check.label}")
            if check.detail:
                lines.append(f"      → {check.detail}")
        if report.passed:
            lines.append(f"  [x] {', '.join(c.label for c in report.passed)}")
        lines.append("⚠  This lineage may not reproduce as recorded — see the unchecked items.")

    for label, url in links or []:
        lines.append(f"  {label}: {url}")
    return "\n".join(lines)
