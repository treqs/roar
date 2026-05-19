"""Best-effort product telemetry hooks for foreground CLI paths."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from . import enqueue_trigger, record_run_result, record_subcommand, resolve_config
from .paths import resolve_paths

LOGGER = logging.getLogger(__name__)

IDENTITY_UPLOAD_TRIGGERS = {
    "first_seen_for_install_id",
    "first_seen_for_version",
}


def maybe_print_telemetry_disclosure(
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Print the one-time telemetry disclosure if the user hasn't seen it.

    Gates, in order:
      1. Telemetry must be enabled — nothing to disclose to users who've
         already opted out via config or `DO_NOT_TRACK`/`ROAR_NO_TELEMETRY`.
      2. Sentinel ``~/.config/roar/.telemetry-disclosed`` must be missing.
      3. Output gate must allow hints — TTY, ``hints.enabled``,
         non-quiet (same gate the brand banner uses).

    When all three pass, prints the two-line hint to stdout via the
    shared hint printer and writes the sentinel. If the gate fails, the
    sentinel is *not* written: a later interactive invocation will show
    the disclosure instead. Any failure is swallowed — telemetry must
    never break CLI use.
    """
    try:
        paths = resolve_paths(environ)
        if paths.disclosure_sentinel_file.exists():
            return
        effective = resolve_config(start_dir=start_dir, environ=environ, paths=paths)
        if not effective.stats_enabled:
            return

        # Lazy import: `roar.cli._format` pulls in click + integrations.config,
        # which we don't want to touch from telemetry except on the first run.
        from ..cli._format import hints_should_print, make_hint_printer

        if not hints_should_print():
            return

        _caps, hint = make_hint_printer()
        hint("Telemetry: anonymous counters (version, commands) — no paths, content, or tokens.")
        hint(
            "  Off: `roar telemetry --disable` or `DO_NOT_TRACK=1`. "
            "Status: `roar telemetry --status`."
        )

        paths.disclosure_sentinel_file.parent.mkdir(parents=True, exist_ok=True)
        paths.disclosure_sentinel_file.touch()
    except Exception as exc:  # pragma: no cover - disclosure must never break CLI use
        LOGGER.debug("Failed to print telemetry disclosure: %s", exc)


def record_cli_subcommand(
    subcommand: str | None,
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Record a top-level CLI invocation without letting telemetry affect UX."""
    if not subcommand or subcommand == "telemetry":
        return

    try:
        result = record_subcommand(subcommand, start_dir=start_dir, environ=environ)
        _enqueue_triggers(
            (trigger for trigger in result.triggers if trigger in IDENTITY_UPLOAD_TRIGGERS),
            start_dir=start_dir,
            environ=environ,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break CLI use
        LOGGER.debug("Failed to record telemetry subcommand: %s", exc)


def record_action_trigger(
    trigger: str,
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Queue an action milestone after the product action has succeeded."""
    try:
        _enqueue_triggers((trigger,), start_dir=start_dir, environ=environ)
    except Exception as exc:  # pragma: no cover - telemetry must never break CLI use
        LOGGER.debug("Failed to queue telemetry action trigger: %s", exc)


def record_run_outcome(
    *,
    success: bool,
    failure_kind: str | None = None,
    tracer_backend: str | None = None,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Record finalizer-aware run outcome counters and queue milestone uploads."""
    try:
        result = record_run_result(
            success=success,
            failure_kind=failure_kind,
            tracer_backend=tracer_backend,
            start_dir=start_dir,
            environ=environ,
        )
        _enqueue_triggers(result.triggers, start_dir=start_dir, environ=environ)
    except Exception as exc:  # pragma: no cover - telemetry must never break CLI use
        LOGGER.debug("Failed to record telemetry run outcome: %s", exc)


def _enqueue_triggers(
    triggers: Iterable[str],
    *,
    start_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    enqueued = False
    for trigger in triggers:
        result = enqueue_trigger(trigger, start_dir=start_dir, environ=environ)
        enqueued = enqueued or result.enqueued
    if enqueued:
        _spawn_uploader(environ=environ)


def _spawn_uploader(*, environ: Mapping[str, str] | None = None) -> None:
    env = None
    if environ is not None:
        env = dict(os.environ)
        env.update(environ)

    try:
        subprocess.Popen(
            [sys.executable, "-m", "roar.telemetry.uploader"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:  # pragma: no cover - upload is opportunistic
        LOGGER.debug("Failed to spawn telemetry uploader: %s", exc)
