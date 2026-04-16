"""Renderer for roar diff output."""

from __future__ import annotations

import json
from typing import Any

from ..application.query.diff_engine import extract_script_and_args
from ..application.query.diff_graph import JobNode
from ..application.query.results import AtomicDiff, DiffCategory, DiffResult


class DiffRenderer:
    """Renders diff results as plain text or JSON."""

    def render_text(self, result: DiffResult, format: str = "summary") -> str:
        if format == "category":
            return self._render_category(result)
        if format == "dag":
            return self._render_dag(result)
        return self._render_summary(result)

    def _render_summary(self, result: DiffResult) -> str:
        lines: list[str] = []

        # Header
        label_a = result.target_a_path or result.ref_a
        label_b = result.target_b_path or result.ref_b
        lines.append(f"Diff: {_short(label_a)} vs {_short(label_b)}")
        lines.append("")

        if result.targets_identical:
            lines.append("Targets are identical (same content hash).")
            return "\n".join(lines)

        if result.target_a_hash and result.target_b_hash:
            lines.append(f"  A: {result.target_a_hash[:16]}...")
            lines.append(f"  B: {result.target_b_hash[:16]}...")
            lines.append("")

        if not result.diffs:
            lines.append("No differences found in lineage.")
            return "\n".join(lines)

        # Root cause callout
        if result.root_cause:
            lines.append(f"ROOT CAUSE: {result.root_cause.description}")
            lines.append("")

        # Group by category, but within each category sort by impact
        by_category: dict[DiffCategory, list[AtomicDiff]] = {}
        for d in result.diffs:
            by_category.setdefault(d.category, []).append(d)

        category_order = [
            DiffCategory.DATA,
            DiffCategory.PARAMS,
            DiffCategory.CODE,
            DiffCategory.COMPUTE,
            DiffCategory.PIPELINE,
        ]

        for cat in category_order:
            cat_diffs = by_category.get(cat)
            if not cat_diffs:
                continue
            lines.append(f"{cat.value.upper()}")
            for d in cat_diffs:
                impact_tag = f" [{d.impact:.2f}]" if d.impact > 0 else ""
                root_tag = " *" if d.is_root_cause else ""
                lines.append(f"  {d.description}{impact_tag}{root_tag}")
            lines.append("")

        # Unchanged summary
        unchanged = []
        if result.matched_jobs:
            all_categories_with_diffs = set(by_category.keys())
            if DiffCategory.CODE not in all_categories_with_diffs:
                unchanged.append("code (same git commit)")
            if DiffCategory.COMPUTE not in all_categories_with_diffs:
                unchanged.append("environment (same packages)")
            if DiffCategory.PARAMS not in all_categories_with_diffs:
                unchanged.append("parameters (same arguments)")
        if unchanged:
            lines.append("UNCHANGED")
            for item in unchanged:
                lines.append(f"  {item}")
            lines.append("")

        # Only show structure section if PIPELINE category wasn't already shown
        if (result.only_in_a or result.only_in_b) and DiffCategory.PIPELINE not in by_category:
            lines.append("STRUCTURE")
            for j in result.only_in_a:
                step = _job_step_label(j)
                lines.append(f"  - {step}: {_truncate(j.command, 60)}")
            for j in result.only_in_b:
                step = _job_step_label(j)
                lines.append(f"  + {step}: {_truncate(j.command, 60)}")

        return "\n".join(lines)

    def _render_category(self, result: DiffResult) -> str:
        """Render diffs grouped strictly by category, no summary framing."""
        lines: list[str] = []

        label_a = result.target_a_path or result.ref_a
        label_b = result.target_b_path or result.ref_b
        lines.append(f"Diff: {_short(label_a)} vs {_short(label_b)}")
        lines.append("")

        if result.targets_identical:
            lines.append("Targets are identical (same content hash).")
            return "\n".join(lines)

        by_category: dict[DiffCategory, list[AtomicDiff]] = {}
        for d in result.diffs:
            by_category.setdefault(d.category, []).append(d)

        for cat in DiffCategory:
            cat_diffs = by_category.get(cat)
            if not cat_diffs:
                continue
            lines.append(f"{cat.value.upper()}")
            for d in cat_diffs:
                marker = " <-- root cause" if d.is_root_cause else ""
                lines.append(f"  [{d.impact:.2f}] {d.description}{marker}")
            lines.append("")

        return "\n".join(lines)

    def _render_dag(self, result: DiffResult) -> str:
        """Render an aligned DAG view showing which steps match and where they diverge."""
        lines: list[str] = []

        label_a = result.target_a_path or result.ref_a
        label_b = result.target_b_path or result.ref_b
        lines.append(f"Diff: {_short(label_a)} vs {_short(label_b)}")
        lines.append("")

        if result.targets_identical:
            lines.append("Targets are identical (same content hash).")
            return "\n".join(lines)

        # Build diff lookup by step label
        diffs_by_step: dict[str, list[AtomicDiff]] = {}
        for d in result.diffs:
            step = d.detail.get("step", "")
            if step:
                diffs_by_step.setdefault(step, []).append(d)

        col_width = 42

        # Header
        lines.append(f"{'A':^{col_width}}  {'B':^{col_width}}")
        lines.append(f"{'-' * col_width}  {'-' * col_width}")

        # Matched jobs in order
        for m in sorted(result.matched_jobs, key=lambda m: m.job_a.step_number or 0):
            ja, jb = m.job_a, m.job_b
            step_a = _job_step_label(ja)
            step_b = _job_step_label(jb)
            script_a, _ = _extract_script(ja.command)
            script_b, _ = _extract_script(jb.command)

            step_diffs = diffs_by_step.get(step_a, [])
            if not step_diffs:
                # Identical step
                label = f"{step_a} {script_a}"
                lines.append(f"{label:<{col_width}}  {f'{step_b} {script_b}':<{col_width}}  =")
            else:
                # Divergent step
                categories = sorted({d.category.value for d in step_diffs})
                diff_summary = ", ".join(categories)
                is_root = any(d.is_root_cause for d in step_diffs)
                marker = " <-- ROOT CAUSE" if is_root else ""
                label_a_str = f"{step_a} {script_a}"
                label_b_str = f"{step_b} {script_b}"
                lines.append(
                    f"{label_a_str:<{col_width}}  {label_b_str:<{col_width}}  "
                    f"DIFFERS ({diff_summary}){marker}"
                )

        # Steps only in A
        for j in result.only_in_a:
            step = _job_step_label(j)
            script, _ = _extract_script(j.command)
            lines.append(f"{f'{step} {script}':<{col_width}}  {'':^{col_width}}  REMOVED")

        # Steps only in B
        for j in result.only_in_b:
            step = _job_step_label(j)
            script, _ = _extract_script(j.command)
            lines.append(f"{'':^{col_width}}  {f'{step} {script}':<{col_width}}  ADDED")

        return "\n".join(lines)

    def render_json(self, result: DiffResult) -> str:
        data: dict[str, Any] = {
            "ref_a": result.ref_a,
            "ref_b": result.ref_b,
            "target_a_hash": result.target_a_hash,
            "target_b_hash": result.target_b_hash,
            "targets_identical": result.targets_identical,
            "root_cause": None,
            "diffs": [
                {
                    "change_type": d.change_type.value,
                    "category": d.category.value,
                    "description": d.description,
                    "detail": d.detail,
                    "impact": d.impact,
                    "is_root_cause": d.is_root_cause,
                }
                for d in result.diffs
            ],
            "matched_jobs": len(result.matched_jobs),
            "only_in_a": len(result.only_in_a),
            "only_in_b": len(result.only_in_b),
        }
        if result.root_cause:
            data["root_cause"] = {
                "description": result.root_cause.description,
                "category": result.root_cause.category.value,
                "impact": result.root_cause.impact,
            }
        return json.dumps(data, indent=2)


def _short(path: str) -> str:
    """Shorten a path for display."""
    parts = path.rsplit("/", 1)
    return parts[-1] if len(parts) > 1 else path


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _job_step_label(job: JobNode) -> str:
    return f"@{job.step_number}" if job.step_number else job.job_uid


def _extract_script(command: str) -> tuple[str, list[str]]:
    """Extract script name from command for DAG view."""
    script, args = extract_script_and_args(command)
    if script:
        return script, args
    return command[:30], []
