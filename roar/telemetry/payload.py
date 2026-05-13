"""Strict allowlisted telemetry payload builder."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .capabilities import (
    collect_feature_capabilities,
    collect_platform,
    normalize_feature_capabilities,
)
from .install import get_roar_version
from .stats import (
    ALLOWED_SUBCOMMAND_COUNTERS,
    ALLOWED_TRACER_SELECTIONS,
    STATS_SCHEMA_VERSION,
    utc_now,
)

PAYLOAD_SCHEMA_VERSION = 1

ALLOWED_TRIGGERS = {
    "first_seen_for_install_id",
    "first_seen_for_version",
    "first_successful_roar_run",
    "second_successful_roar_run",
    "third_successful_roar_run",
    "init",
    "reset",
    "register",
    "preview",
}

UPLOAD_TRIGGERS = ALLOWED_TRIGGERS - {"preview"}

ALLOWED_TOP_LEVEL_FIELDS = (
    "payload_schema_version",
    "stats_schema_version",
    "event_id",
    "install_id",
    "sequence",
    "first_seen",
    "trigger",
    "trigger_ts",
    "counters_as_of",
    "version",
    "platform",
    "subcommand_counts",
    "tracer_selections",
    "feature_capabilities",
)

ALLOWED_PLATFORM_FIELDS = {
    "os",
    "arch",
    "python",
    "shell",
    "installer",
    "containerized",
}


def build_payload(
    trigger: str,
    stats_snapshot: Mapping[str, Any],
    *,
    event_id: str | None = None,
    trigger_ts: str | None = None,
    version: str | None = None,
    endpoint: str | None = None,
    platform_info: Mapping[str, Any] | None = None,
    feature_capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a telemetry payload from a local stats snapshot.

    Only fields named in this module's allowlists can enter the returned payload.
    Unknown stats, config, environment, path, command, and integration fields are ignored.
    """

    normalized_trigger = str(trigger or "").strip()
    if normalized_trigger not in ALLOWED_TRIGGERS:
        raise ValueError(f"unknown telemetry trigger: {normalized_trigger or '<empty>'}")

    timestamp = trigger_ts or utc_now()
    payload = {
        "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
        "stats_schema_version": int(
            stats_snapshot.get("stats_schema_version") or STATS_SCHEMA_VERSION
        ),
        "event_id": event_id or str(uuid4()),
        "install_id": str(stats_snapshot.get("install_id") or ""),
        "sequence": int(stats_snapshot.get("sequence") or 0),
        "first_seen": str(stats_snapshot.get("first_seen") or timestamp),
        "trigger": normalized_trigger,
        "trigger_ts": timestamp,
        "counters_as_of": str(stats_snapshot.get("updated_at") or timestamp),
        "version": version or get_roar_version(),
        "platform": _allowlisted_platform(platform_info or collect_platform()),
        "subcommand_counts": _allowlisted_int_map(
            stats_snapshot.get("subcommand_counts"),
            ALLOWED_SUBCOMMAND_COUNTERS,
        ),
        "tracer_selections": _allowlisted_int_map(
            stats_snapshot.get("tracer_selections"),
            ALLOWED_TRACER_SELECTIONS,
        ),
        "feature_capabilities": normalize_feature_capabilities(
            feature_capabilities or collect_feature_capabilities(endpoint)
        ),
    }
    return {key: payload[key] for key in ALLOWED_TOP_LEVEL_FIELDS}


def payload_to_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), indent=2, sort_keys=True)


def _allowlisted_int_map(value: object, allowed_keys: set[str]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}

    result: dict[str, int] = {}
    for key in sorted(allowed_keys):
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw >= 0:
            result[key] = raw
    return result


def _allowlisted_platform(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ALLOWED_PLATFORM_FIELDS:
        if key not in values:
            continue
        if key == "containerized":
            result[key] = bool(values[key])
        else:
            result[key] = str(values[key])
    return result
