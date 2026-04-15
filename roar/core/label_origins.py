"""Helpers for deriving per-key label origins from local label history."""

from __future__ import annotations

import json
from typing import Any, Literal

LabelOrigin = Literal["user", "system"]

LABEL_ORIGIN_USER: LabelOrigin = "user"
LABEL_ORIGIN_SYSTEM: LabelOrigin = "system"


def build_current_key_origins(history_rows: list[dict[str, Any]]) -> dict[str, LabelOrigin]:
    """Replay version history to recover the current origin of each label key path."""
    key_origins: dict[str, LabelOrigin] = {}
    previous_values: dict[str, str] = {}

    for row in history_rows:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        current_values = _flatten_leaf_values(metadata)
        changed_paths = sorted(set(previous_values).union(current_values))
        row_origin = _normalize_row_origin(row.get("write_origin"))

        for path in changed_paths:
            previous_value = previous_values.get(path)
            current_value = current_values.get(path)
            if previous_value == current_value:
                continue
            if current_value is None:
                key_origins.pop(path, None)
                continue
            key_origins[path] = row_origin or infer_legacy_key_origin(path)

        previous_values = current_values

    return {path: key_origins[path] for path in sorted(key_origins)}


def infer_legacy_key_origin(path: str) -> LabelOrigin:
    """Best-effort fallback for rows created before explicit origin tracking."""
    if path == "dataset" or path.startswith("dataset."):
        return LABEL_ORIGIN_SYSTEM
    return LABEL_ORIGIN_USER


def _normalize_row_origin(value: Any) -> LabelOrigin | None:
    if value == LABEL_ORIGIN_SYSTEM:
        return LABEL_ORIGIN_SYSTEM
    if value == LABEL_ORIGIN_USER:
        return LABEL_ORIGIN_USER
    return None


def _flatten_leaf_values(metadata: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}

    def _walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key in sorted(value.keys()):
                next_prefix = f"{prefix}.{key}" if prefix else key
                _walk(next_prefix, value[key])
            return
        if not prefix:
            return
        flat[prefix] = json.dumps(value, sort_keys=True, separators=(",", ":"))

    _walk("", metadata)
    return flat
