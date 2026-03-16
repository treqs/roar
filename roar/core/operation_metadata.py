"""Helpers for serializing operation metadata payloads."""

from __future__ import annotations

import json
from typing import Any


def build_operation_metadata_json(operation: str, payload: dict[str, Any]) -> str:
    """Wrap operation payload in a namespaced metadata object and serialize to JSON."""
    return json.dumps({operation: payload})
