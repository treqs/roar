"""Lazy install of a per-ABI runtime tree for cross-Python ``roar run``.

When ``uv tool install roar-cli`` installs roar under one Python (e.g. 3.13)
but ``roar run`` is invoked against a different one (e.g. system 3.12),
roar's bundled compiled deps don't match the traced Python's ABI. This
module installs a matching tree of runtime deps on demand into a per-ABI
cache directory under ``~/.cache/roar/runtime/<tag>/``.

``sitecustomize.py``'s ``_append_roar_runtime_pythonpath`` prepends the
cache directory to ``sys.path`` in the traced process, so imports there
resolve to the ABI-matched copies before reaching roar's bundled tree.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Deps backend dispatch needs in the traced Python. Kept short — pip/uv
# resolves transitive deps. Unpinned: roar tolerates any pydantic 2.x.
_RUNTIME_DEPS: tuple[str, ...] = ("pydantic", "blake3")

_STAMP_FILENAME = "roar_runtime.json"
_INSTALL_TIMEOUT_SECONDS = 180


def runtime_cache_root() -> Path:
    """Return ``$XDG_CACHE_HOME/roar/runtime`` (default ``~/.cache/roar/runtime``)."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
    return base / "roar" / "runtime"


def runtime_cache_dir(abi_tag: str) -> Path:
    """Return the per-ABI cache directory (e.g. ``.../roar/runtime/cp312/``)."""
    return runtime_cache_root() / abi_tag


def runtime_site_packages(abi_tag: str) -> Path:
    return runtime_cache_dir(abi_tag) / "site-packages"


def is_runtime_cached(abi_tag: str, roar_version: str) -> bool:
    """True iff a matching, roar-version-stamped runtime tree exists for ``abi_tag``."""
    stamp_path = runtime_cache_dir(abi_tag) / _STAMP_FILENAME
    if not stamp_path.is_file():
        return False
    try:
        stamp = json.loads(stamp_path.read_text())
    except (OSError, ValueError):
        return False
    return stamp.get("roar_version") == roar_version


def install_runtime(
    abi_tag: str,
    target_python: str,
    roar_version: str,
    deps: tuple[str, ...] = _RUNTIME_DEPS,
) -> bool:
    """Install a matching runtime tree for ``abi_tag``. Returns ``True`` on success.

    Atomic: installs into a tempdir alongside the cache root, then renames
    into place. Failures (no network, missing pip, etc.) leave the cache in
    its prior state — callers should treat a ``False`` return as "fall back
    to the sitecustomize gate."
    """
    cache_dir = runtime_cache_dir(abi_tag)
    sys.stderr.write(
        f"🦖 installing roar runtime for {abi_tag} ... (one-time per Python; cached)\n"
    )
    sys.stderr.flush()

    try:
        cache_root = runtime_cache_root()
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    tmpdir = Path(tempfile.mkdtemp(prefix="roar-runtime-", dir=cache_root))
    moved = False
    try:
        target_site = tmpdir / "site-packages"
        target_site.mkdir(parents=True)
        installer_cmd = _select_installer(target_python, target_site, deps)
        if installer_cmd is None:
            sys.stderr.write("🦖 install failed: no installer found (need uv or pip)\n")
            return False
        try:
            result = subprocess.run(
                installer_cmd,
                capture_output=True,
                text=True,
                timeout=_INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(f"🦖 install failed: {exc}\n")
            return False
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-500:]
            sys.stderr.write(f"🦖 install failed (rc={result.returncode}): {stderr_tail}\n")
            return False

        stamp_data = {
            "roar_version": roar_version,
            "abi_tag": abi_tag,
            "installed_at": time.time(),
            "deps": list(deps),
        }
        (tmpdir / _STAMP_FILENAME).write_text(json.dumps(stamp_data, indent=2))

        if cache_dir.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(cache_dir)
        try:
            os.rename(tmpdir, cache_dir)
            moved = True
        except OSError:
            return False
    finally:
        if not moved:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmpdir)

    return is_runtime_cached(abi_tag, roar_version)


def _select_installer(
    target_python: str, target_dir: Path, deps: tuple[str, ...]
) -> list[str] | None:
    """Pick the install command. Prefer ``uv pip install --target --python``."""
    uv = shutil.which("uv")
    if uv:
        return [
            uv,
            "pip",
            "install",
            "--target",
            str(target_dir),
            "--python",
            target_python,
            *deps,
        ]
    # Fallback: plain pip. Only works if `pip` is in the target Python's env;
    # best-effort. uv is strongly preferred because of the --python flag.
    pip = shutil.which("pip") or shutil.which("pip3")
    if pip:
        return [pip, "install", "--target", str(target_dir), *deps]
    return None


def runtime_install_mode(start_dir: Path | None = None) -> str:
    """Resolve runtime.install mode: ``'auto'`` (default) or ``'skip'``.

    ``ROAR_RUNTIME_INSTALL`` env var takes precedence over project config.
    Anything unrecognized falls back to ``'auto'``.
    """
    env_value = os.environ.get("ROAR_RUNTIME_INSTALL")
    if env_value:
        normalized = env_value.strip().lower()
        if normalized in ("auto", "skip"):
            return normalized
        return "auto"

    try:
        from roar.integrations.config.access import config_get

        configured = config_get(
            "runtime.install", start_dir=str(start_dir) if start_dir is not None else None
        )
    except Exception:
        return "auto"
    if isinstance(configured, str) and configured.lower() in ("auto", "skip"):
        return configured.lower()
    return "auto"


def ensure_runtime(
    target_python: str,
    target_abi: str,
    bundled_abi: str | None,
    roar_version: str,
    mode: str | None = None,
    start_dir: Path | None = None,
) -> Path | None:
    """Return the site-packages path for an ABI-matched runtime, or ``None``.

    Behavior:
    - No action when ``target_abi`` matches ``bundled_abi`` — bundled wins.
    - No action when mode is ``'skip'`` — gate handles whatever happens.
    - Cache hit: return the cached path.
    - Cache miss: install lazily and return the path on success.
    - Install failure: ``None`` (caller falls back to the gate).
    """
    if not target_abi:
        return None
    if bundled_abi and target_abi == bundled_abi:
        return None

    resolved_mode = mode or runtime_install_mode(start_dir)
    if resolved_mode == "skip":
        return None

    if is_runtime_cached(target_abi, roar_version):
        return runtime_site_packages(target_abi)

    if install_runtime(target_abi, target_python, roar_version):
        return runtime_site_packages(target_abi)
    return None
