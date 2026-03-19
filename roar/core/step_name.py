"""Helpers for resolving and storing the user-facing step name."""

from __future__ import annotations

import json
from typing import Any

STEP_NAME_LABEL_KEY = "name"


def get_step_name_label(metadata: dict[str, Any] | None) -> str | None:
    """Return the scalar job-name label value when present."""
    if not metadata or STEP_NAME_LABEL_KEY not in metadata:
        return None

    value = metadata[STEP_NAME_LABEL_KEY]
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def resolve_step_name(
    label_metadata: dict[str, Any] | None,
    legacy_step_name: str | None,
) -> str | None:
    """Prefer the canonical label-backed step name, with legacy fallback."""
    return get_step_name_label(label_metadata) or legacy_step_name


def omit_step_name_label(
    metadata: dict[str, Any] | None,
    *,
    step_name: str | None,
) -> dict[str, Any] | None:
    """Drop the canonical name label when it is already rendered separately."""
    if not metadata:
        return None
    if step_name is None or get_step_name_label(metadata) != step_name:
        return metadata

    filtered = {key: value for key, value in metadata.items() if key != STEP_NAME_LABEL_KEY}
    return filtered or None
