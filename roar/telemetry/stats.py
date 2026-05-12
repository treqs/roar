"""Local telemetry stats mutation."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ._io import atomic_write_json, file_lock, read_json_object
from .install import get_roar_version
from .paths import TelemetryPaths, resolve_paths

STATS_SCHEMA_VERSION = 1

ALLOWED_SUBCOMMAND_COUNTERS = {
    "run",
    "run_success",
    "run_failed_dirty",
    "run_failed_tracer_setup",
    "run_failed_user_exit",
    "run_failed_interrupted",
    "run_failed_internal",
    "build",
    "init",
    "reset",
    "register",
    "show",
    "dag",
    "lineage",
    "diff",
    "get",
    "put",
    "log",
    "status",
    "inputs",
    "reproduce",
    "projects",
    "auth",
    "config",
    "env",
    "label",
    "login",
    "logout",
    "osmo",
    "pop",
    "proxy",
    "telemetry",
    "tracer",
    "tui",
    "whoami",
    "workflow",
}

RUN_FAILURE_COUNTERS = {
    "dirty": "run_failed_dirty",
    "tracer_setup": "run_failed_tracer_setup",
    "user_exit": "run_failed_user_exit",
    "interrupted": "run_failed_interrupted",
    "internal": "run_failed_internal",
}

ALLOWED_TRACER_SELECTIONS = {"preload", "ptrace", "ebpf", "native", "auto", "disabled", "unknown"}

SUBCOMMAND_UPLOAD_TRIGGERS = {
    "init": "init",
    "reset": "reset",
    "register": "register",
}

RUN_SUCCESS_MILESTONE_TRIGGERS = {
    1: "first_successful_roar_run",
    2: "second_successful_roar_run",
    3: "third_successful_roar_run",
}


@dataclass(frozen=True)
class StatsMutationResult:
    recorded: bool
    stats: dict[str, Any] | None
    triggers: tuple[str, ...] = ()
    reason: str | None = None


def read_stats(paths: TelemetryPaths | None = None) -> dict[str, Any] | None:
    resolved_paths = paths or resolve_paths()
    data = read_json_object(resolved_paths.stats_file)
    return data or None


def preview_stats(version: str | None = None) -> dict[str, Any]:
    now = utc_now()
    return _new_stats(now=now, version=version or get_roar_version())


def ensure_stats(
    *,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> StatsMutationResult:
    resolved_paths = paths or resolve_paths()
    resolved_version = version or get_roar_version()

    def mutator(stats: dict[str, Any], context: _MutationContext) -> tuple[str, ...]:
        triggers = list(_ensure_identity_and_version(stats, context, resolved_version))
        return tuple(triggers)

    stats, triggers = _mutate_stats(resolved_paths, mutator, version=resolved_version)
    return StatsMutationResult(recorded=True, stats=stats, triggers=triggers)


def record_subcommand(
    subcommand: str,
    *,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> StatsMutationResult:
    normalized = str(subcommand or "").strip().lower()
    if normalized not in ALLOWED_SUBCOMMAND_COUNTERS:
        return StatsMutationResult(recorded=False, stats=None, reason="unknown_subcommand")

    resolved_paths = paths or resolve_paths()
    resolved_version = version or get_roar_version()

    def mutator(stats: dict[str, Any], context: _MutationContext) -> tuple[str, ...]:
        triggers = list(_ensure_identity_and_version(stats, context, resolved_version))
        _increment_counter(stats.setdefault("subcommand_counts", {}), normalized)
        stats["updated_at"] = context.now
        trigger = SUBCOMMAND_UPLOAD_TRIGGERS.get(normalized)
        if trigger is not None:
            triggers.append(trigger)
        return tuple(triggers)

    stats, triggers = _mutate_stats(resolved_paths, mutator, version=resolved_version)
    return StatsMutationResult(recorded=True, stats=stats, triggers=triggers)


def record_run_result(
    *,
    success: bool,
    failure_kind: str | None = None,
    tracer_backend: str | None = None,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> StatsMutationResult:
    resolved_paths = paths or resolve_paths()
    resolved_version = version or get_roar_version()

    def mutator(stats: dict[str, Any], context: _MutationContext) -> tuple[str, ...]:
        triggers = list(_ensure_identity_and_version(stats, context, resolved_version))
        subcommand_counts = stats.setdefault("subcommand_counts", {})
        if success:
            success_count = _increment_counter(subcommand_counts, "run_success")
            trigger = RUN_SUCCESS_MILESTONE_TRIGGERS.get(success_count)
            if trigger is not None:
                triggers.append(trigger)
        else:
            failure_counter = RUN_FAILURE_COUNTERS.get(
                str(failure_kind or "internal").strip().lower(),
                "run_failed_internal",
            )
            _increment_counter(subcommand_counts, failure_counter)

        normalized_tracer = _normalize_tracer_backend(tracer_backend)
        if normalized_tracer is not None:
            _increment_counter(stats.setdefault("tracer_selections", {}), normalized_tracer)
        stats["updated_at"] = context.now
        return tuple(triggers)

    stats, triggers = _mutate_stats(resolved_paths, mutator, version=resolved_version)
    return StatsMutationResult(recorded=True, stats=stats, triggers=triggers)


def advance_sequence(
    *,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or resolve_paths()
    resolved_version = version or get_roar_version()

    def mutator(stats: dict[str, Any], context: _MutationContext) -> tuple[str, ...]:
        _ensure_identity_and_version(stats, context, resolved_version)
        stats["sequence"] = int(stats.get("sequence") or 0) + 1
        stats["updated_at"] = context.now
        return ()

    stats, _triggers = _mutate_stats(resolved_paths, mutator, version=resolved_version)
    return stats


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _MutationContext:
    now: str
    created: bool


def _mutate_stats(
    paths: TelemetryPaths,
    mutator: Callable[[dict[str, Any], _MutationContext], tuple[str, ...]],
    *,
    version: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    with file_lock(paths.lock_file):
        current = read_json_object(paths.stats_file)
        now = utc_now()
        created = not bool(current)
        stats = _normalize_stats(current, now=now, version=version)
        triggers = mutator(stats, _MutationContext(now=now, created=created))
        atomic_write_json(paths.stats_file, stats)
        return copy.deepcopy(stats), triggers


def _normalize_stats(data: Mapping[str, Any], *, now: str, version: str) -> dict[str, Any]:
    stats = dict(data)
    stats["stats_schema_version"] = STATS_SCHEMA_VERSION
    stats.setdefault("install_id", str(uuid4()))
    stats.setdefault("first_seen", now)
    stats["sequence"] = int(stats.get("sequence") or 0)
    _ensure_mapping(stats, "version_milestones")
    _ensure_mapping(stats, "subcommand_counts")
    _ensure_mapping(stats, "tracer_selections")
    _ensure_mapping(stats, "feature_capabilities")
    return stats


def _new_stats(*, now: str, version: str) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "stats_schema_version": STATS_SCHEMA_VERSION,
        "install_id": str(uuid4()),
        "first_seen": now,
        "sequence": 0,
        "version_milestones": {},
        "subcommand_counts": {},
        "tracer_selections": {},
        "feature_capabilities": {},
        "updated_at": now,
    }
    _ensure_identity_and_version(stats, _MutationContext(now=now, created=True), version)
    return stats


def _ensure_identity_and_version(
    stats: dict[str, Any],
    context: _MutationContext,
    version: str,
) -> tuple[str, ...]:
    triggers: list[str] = []
    if context.created:
        triggers.append("first_seen_for_install_id")
    version_milestones = stats.setdefault("version_milestones", {})
    if isinstance(version_milestones, dict) and version not in version_milestones:
        version_milestones[version] = {"first_seen": context.now}
        triggers.append("first_seen_for_version")
    return tuple(triggers)


def _ensure_mapping(stats: dict[str, Any], key: str) -> None:
    if not isinstance(stats.get(key), dict):
        stats[key] = {}


def _increment_counter(counters: dict[str, Any], key: str) -> int:
    current = counters.get(key)
    value = int(current) if isinstance(current, int) else 0
    value += 1
    counters[key] = value
    return value


def _normalize_tracer_backend(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return normalized if normalized in ALLOWED_TRACER_SELECTIONS else "unknown"
