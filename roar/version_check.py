"""Best-effort "a newer roar is available" nudge.

Design constraints (all deliberate):

* **Zero foreground latency.** The pypi lookup runs *only* in the background
  telemetry uploader subprocess (``roar.telemetry.uploader``), which is already
  detached and fire-and-forget. The foreground (the ``hint:`` line) only ever
  reads a small on-disk cache — the command path never touches the network.
* **Cached, throttled.** The lookup is skipped when the cache is younger than
  ``_TTL_SECONDS`` so we don't hammer pypi on every telemetry flush.
* **Fail-open, always.** Every path swallows its exceptions. A version check
  must never block, slow, or break the CLI — if pypi is slow/unreachable or the
  cache is corrupt, the nudge simply doesn't appear.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .telemetry.paths import resolve_paths

_PYPI_URL = "https://pypi.org/pypi/roar-cli/json"
_TTL_SECONDS = 24 * 60 * 60
_TIMEOUT_SECONDS = 3.0


def _cache_path(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_paths(environ).cache_dir / "version_check.json"


def _read_cache(environ: Mapping[str, str] | None = None) -> dict | None:
    try:
        return json.loads(_cache_path(environ).read_text(encoding="utf-8"))
    except Exception:
        return None


def refresh_pypi_version_cache(
    environ: Mapping[str, str] | None = None,
    *,
    force: bool = False,
) -> None:
    """Fetch the latest ``roar-cli`` version from pypi and cache it.

    Intended to be called only from the background telemetry uploader. No-op
    (no network) when the cache is younger than the TTL unless ``force``.
    Swallows every error.
    """
    try:
        cached = _read_cache(environ)
        if (
            not force
            and cached
            and (time.time() - float(cached.get("checked_at", 0))) < _TTL_SECONDS
        ):
            return
        request = urllib.request.Request(_PYPI_URL, headers={"User-Agent": "roar-version-check/1"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latest = str(payload["info"]["version"])
        path = _cache_path(environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        return  # fail-open: a version check must never raise


def pending_upgrade_version(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the cached latest version when it's newer than the running
    version, else ``None``. Reads the cache only (never the network)."""
    try:
        cached = _read_cache(environ)
        if not cached:
            return None
        latest = str(cached.get("latest") or "")
        if not latest:
            return None
        from packaging.version import InvalidVersion, Version

        try:
            return latest if Version(latest) > Version(__version__) else None
        except InvalidVersion:
            return None
    except Exception:
        return None


def upgrade_hint_text(environ: Mapping[str, str] | None = None) -> str | None:
    """Ready-to-print ``hint:`` body when a newer roar is available, else None."""
    latest = pending_upgrade_version(environ)
    if not latest:
        return None
    return (
        f"roar {latest} is available (you have {__version__}). "
        f"Upgrade with `uv tool upgrade roar-cli` or `pip install -U roar-cli`."
    )
