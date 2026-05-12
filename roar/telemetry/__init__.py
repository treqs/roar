"""Client-side product telemetry core APIs.

Foreground callers can resolve status, mutate local stats, build inspectable
payloads, and enqueue uploads. Network work lives only in ``roar.telemetry.uploader``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import payload, queue, stats
from .config import (
    EffectiveTelemetryConfig,
    resolve_config,
    set_global_enabled,
    set_global_endpoint,
)
from .paths import TelemetryPaths, resolve_paths
from .queue import QueueResult
from .stats import StatsMutationResult


def status_snapshot(
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    stats_snapshot = stats.read_stats(resolved_paths)
    return {
        "enabled": effective.stats_enabled,
        "uploads_enabled": effective.uploads_enabled,
        "suppression_reason": effective.suppression_reason,
        "disabled_reason": effective.disabled_reason,
        "endpoint": effective.endpoint,
        "endpoint_source": effective.endpoint_source,
        "enabled_source": effective.enabled_source,
        "queued_event_count": queue.queued_event_count(resolved_paths),
        "install_id": (stats_snapshot or {}).get("install_id"),
        "stats_file": str(resolved_paths.stats_file),
        "queue_dir": str(resolved_paths.queue_dir),
        "global_config_file": str(effective.global_config_path),
        "project_config_file": str(effective.project_config_path)
        if effective.project_config_path is not None
        else None,
    }


def print_payload(
    *,
    trigger: str = "preview",
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> str:
    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    stats_snapshot = stats.read_stats(resolved_paths) or stats.preview_stats(version)
    built_payload = payload.build_payload(
        trigger,
        stats_snapshot,
        endpoint=effective.endpoint,
        version=version,
    )
    return payload.payload_to_json(built_payload)


def record_subcommand(
    subcommand: str,
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> StatsMutationResult:
    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    if not effective.stats_enabled:
        return StatsMutationResult(
            recorded=False,
            stats=None,
            reason=_disabled_reason(effective),
        )
    return stats.record_subcommand(subcommand, paths=resolved_paths, version=version)


def record_run_result(
    *,
    success: bool,
    failure_kind: str | None = None,
    tracer_backend: str | None = None,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> StatsMutationResult:
    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    if not effective.stats_enabled:
        return StatsMutationResult(
            recorded=False,
            stats=None,
            reason=_disabled_reason(effective),
        )
    return stats.record_run_result(
        success=success,
        failure_kind=failure_kind,
        tracer_backend=tracer_backend,
        paths=resolved_paths,
        version=version,
    )


def enqueue_trigger(
    trigger: str,
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
    version: str | None = None,
) -> QueueResult:
    normalized_trigger = str(trigger or "").strip()
    if normalized_trigger not in payload.ALLOWED_TRIGGERS:
        raise ValueError(f"unknown telemetry trigger: {normalized_trigger or '<empty>'}")

    resolved_paths = paths or resolve_paths(environ)
    effective = resolve_config(start_dir=start_dir, environ=environ, paths=resolved_paths)
    if not effective.stats_enabled:
        return QueueResult(
            event_id="",
            path=None,
            enqueued=False,
            reason=_disabled_reason(effective),
        )
    if not effective.endpoint:
        return QueueResult(event_id="", path=None, enqueued=False, reason="empty_endpoint")

    stats_snapshot = stats.advance_sequence(paths=resolved_paths, version=version)
    built_payload = payload.build_payload(
        normalized_trigger,
        stats_snapshot,
        endpoint=effective.endpoint,
        version=version,
    )
    return queue.enqueue_payload(built_payload, paths=resolved_paths)


def _disabled_reason(effective: EffectiveTelemetryConfig) -> str:
    return effective.suppression_reason or effective.disabled_reason or "disabled"


__all__ = [
    "EffectiveTelemetryConfig",
    "QueueResult",
    "StatsMutationResult",
    "TelemetryPaths",
    "enqueue_trigger",
    "print_payload",
    "record_run_result",
    "record_subcommand",
    "resolve_config",
    "resolve_paths",
    "set_global_enabled",
    "set_global_endpoint",
    "status_snapshot",
]
