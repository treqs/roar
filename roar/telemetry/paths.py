"""Filesystem paths for roar product telemetry."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelemetryPaths:
    """Resolved XDG-aware telemetry paths."""

    config_home: Path
    cache_home: Path
    global_config_file: Path
    cache_dir: Path
    telemetry_dir: Path
    stats_file: Path
    queue_dir: Path
    lock_file: Path


def resolve_paths(environ: Mapping[str, str] | None = None) -> TelemetryPaths:
    """Resolve all telemetry paths using XDG config/cache conventions."""

    resolved_env = os.environ if environ is None else environ
    home = Path(resolved_env.get("HOME") or Path.home()).expanduser()
    config_home = _xdg_dir(resolved_env.get("XDG_CONFIG_HOME"), home / ".config")
    cache_home = _xdg_dir(resolved_env.get("XDG_CACHE_HOME"), home / ".cache")

    cache_dir = cache_home / "roar"
    telemetry_dir = cache_dir / "telemetry"
    return TelemetryPaths(
        config_home=config_home,
        cache_home=cache_home,
        global_config_file=config_home / "roar" / "config.toml",
        cache_dir=cache_dir,
        telemetry_dir=telemetry_dir,
        stats_file=cache_dir / "stats.json",
        queue_dir=telemetry_dir / "queue",
        lock_file=telemetry_dir / "telemetry.lock",
    )


def global_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).global_config_file


def cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).cache_dir


def stats_path(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).stats_file


def queue_dir(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).queue_dir


def lock_path(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).lock_file


def _xdg_dir(value: str | None, default: Path) -> Path:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return default
