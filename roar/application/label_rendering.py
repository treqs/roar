"""Pure helpers for rendering label metadata without DB-side imports."""

from __future__ import annotations

import json
from typing import Any


def flatten_label_metadata(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten metadata into sorted ``(key, display_value)`` pairs."""
    flat: list[tuple[str, str]] = []

    def _walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key in sorted(value.keys()):
                next_prefix = f"{prefix}.{key}" if prefix else key
                _walk(next_prefix, value[key])
            return
        flat.append((prefix, _display_scalar(value)))

    _walk("", metadata)
    return flat


def render_label_lines(metadata: dict[str, Any], indent: str = "") -> list[str]:
    """Render a metadata document as sorted ``key=value`` lines."""
    return [f"{indent}{key}={value}" for key, value in flatten_label_metadata(metadata)]


def _display_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True)
