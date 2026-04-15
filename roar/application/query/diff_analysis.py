"""Optional LLM-assisted analysis helpers for `roar diff`."""

from __future__ import annotations

import json
import os
from typing import Any

from .results import DiffResult


def generate_llm_analysis(result: DiffResult) -> str | None:
    """Send the structured diff to Claude for natural-language interpretation."""
    try:
        import anthropic
    except ImportError:
        return "(--analyze requires the 'anthropic' package: pip install anthropic)"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(--analyze requires ANTHROPIC_API_KEY environment variable)"

    diff_data: dict[str, Any] = {
        "ref_a": result.ref_a,
        "ref_b": result.ref_b,
        "target_a_hash": result.target_a_hash,
        "target_b_hash": result.target_b_hash,
        "targets_identical": result.targets_identical,
        "diffs": [
            {
                "change_type": diff.change_type.value,
                "category": diff.category.value,
                "description": diff.description,
                "impact": diff.impact,
                "is_root_cause": diff.is_root_cause,
            }
            for diff in result.diffs
        ],
        "matched_steps": len(result.matched_jobs),
        "steps_only_in_a": len(result.only_in_a),
        "steps_only_in_b": len(result.only_in_b),
    }
    if result.root_cause:
        diff_data["root_cause"] = {
            "description": result.root_cause.description,
            "category": result.root_cause.category.value,
            "impact": result.root_cause.impact,
        }

    prompt = f"""Analyze this ML pipeline provenance diff and explain what changed and why it matters.

The diff compares two artifacts produced by ML pipelines tracked by 'roar', a provenance tool.
Each diff has a category (data, params, code, compute, pipeline), a change_type, and an
impact score (0-1, higher = more likely to be the root cause).

Structured diff:
{json.dumps(diff_data, indent=2)}

Provide a concise (3-5 sentence) analysis:
1. What is the primary reason these artifacts differ?
2. Which changes are root causes vs. consequences?
3. Any concerns (e.g., non-determinism, environment drift)?

Be direct and specific. Reference step numbers and file names."""

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        return f"(LLM analysis failed: {exc})"
