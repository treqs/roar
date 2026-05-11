"""One-time per-machine banner for tracer backend selection.

When `roar run` falls back from eBPF to preload (the common case on dev
machines without `CAP_BPF`), users currently see a success banner and
have no idea they're on a limited-coverage backend. This module emits a
one-time banner explaining the selection and how to upgrade — fired
either the first time a backend is used on this machine or any time a
backend is selected via fallback (i.e. user asked for `auto` and got
something other than the preferred eBPF).

Suppress with `roar config set tracer.banner false`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO

from ...integrations.config import config_get

# Order matches AUTO_BACKEND_ORDER: preferred → least-preferred.
_PREFERRED_AUTO_BACKEND = "ebpf"


def _state_dir() -> Path:
    """Per-machine state directory.

    Honors $XDG_STATE_HOME on Linux; falls back to `~/.local/state`. macOS
    follows the same convention since there's no platform-blessed
    alternative for non-cache state and roar already uses XDG-style dirs
    elsewhere.
    """
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "roar"
    return Path.home() / ".local" / "state" / "roar"


def _seen_path() -> Path:
    return _state_dir() / "tracer_banners.json"


def _load_seen() -> set[str]:
    path = _seen_path()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    backends = data.get("seen_backends") if isinstance(data, dict) else None
    if not isinstance(backends, list):
        return set()
    return {b for b in backends if isinstance(b, str)}


def _save_seen(seen: set[str]) -> None:
    path = _seen_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"seen_backends": sorted(seen)}, f)
    except OSError:
        # Best-effort: a writable state dir isn't required, just nice to
        # have. If we can't persist, the banner will reappear next run.
        pass


def _config_banner_enabled() -> bool:
    value = config_get("tracer.banner")
    if isinstance(value, bool):
        return value
    return True


_PRELOAD_BANNER = (
    "Selected preload tracer (eBPF requires CAP_BPF — not granted). "
    "Tracer may miss writes through chained shell pipelines. "
    "Full coverage: sudo setcap cap_bpf,cap_perfmon+ep $(which roar-tracer-ebpf). "
    "Suppress: roar config set tracer.banner false."
)

_PTRACE_BANNER = (
    "Selected ptrace tracer (eBPF + preload not available). "
    "Tracer adds modest per-syscall overhead but has full coverage. "
    "Faster: sudo setcap cap_bpf,cap_perfmon+ep $(which roar-tracer-ebpf). "
    "Suppress: roar config set tracer.banner false."
)

_EBPF_BANNER = (
    "Selected eBPF tracer (full coverage, low overhead). "
    "Suppress: roar config set tracer.banner false."
)


def _banner_for(backend: str) -> str | None:
    if backend == "preload":
        return _PRELOAD_BANNER
    if backend == "ptrace":
        return _PTRACE_BANNER
    if backend == "ebpf":
        return _EBPF_BANNER
    return None


def emit_banner_if_needed(
    selected_backend: str,
    requested_mode: str | None,
    *,
    stream: IO[str] | None = None,
) -> bool:
    """Print a tracer-selection banner to `stream` (default stderr) when
    appropriate. Returns True if a banner was printed.

    Banner fires when EITHER:
      - This is the first time this backend has been used on this machine
        (per-backend state at $XDG_STATE_HOME/roar/tracer_banners.json), OR
      - The backend was selected via fallback — i.e. `requested_mode` is
        `None`/`"auto"` and `selected_backend` isn't the auto preferred
        backend (eBPF). This catches the headline case: dev machine
        without CAP_BPF silently falling through to preload.

    Fully suppressed by `roar config set tracer.banner false`.
    """
    if not _config_banner_enabled():
        return False

    text = _banner_for(selected_backend)
    if text is None:
        return False

    is_fallback = requested_mode in (None, "auto") and (selected_backend != _PREFERRED_AUTO_BACKEND)
    seen = _load_seen()
    is_first_time = selected_backend not in seen

    if not (is_fallback or is_first_time):
        return False

    if stream is None:
        import sys

        stream = sys.stderr
    stream.write(text + "\n")
    stream.flush()

    if is_first_time:
        seen.add(selected_backend)
        _save_seen(seen)

    return True
