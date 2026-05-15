from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import threading

_roar_suppress = threading.local()


def bundled_abi_tag(inject_dir: str) -> str | None:
    """Return the cpython ABI tag of roar's bundled compiled deps, or None.

    Walks up from ``inject_dir`` to find the enclosing ``site-packages`` and
    parses the CPython ABI tag from a known compiled dependency's ``.so``
    filename (e.g. ``_pydantic_core.cpython-313-x86_64-linux-gnu.so`` →
    ``"cpython-313"``). Returns ``None`` if the layout doesn't match — callers
    should treat ``None`` as "don't gate" rather than assuming mismatch.
    """
    site_pkg = inject_dir
    while os.path.basename(site_pkg) != "site-packages":
        parent = os.path.dirname(site_pkg)
        if parent == site_pkg:
            return None
        site_pkg = parent

    for known_pkg in ("pydantic_core", "blake3"):
        pkg_dir = os.path.join(site_pkg, known_pkg)
        if not os.path.isdir(pkg_dir):
            continue
        try:
            entries = os.listdir(pkg_dir)
        except OSError:
            continue
        for filename in entries:
            for token in filename.split("."):
                if token.startswith("cpython-"):
                    m = re.match(r"cpython-\d+", token)
                    if m:
                        return m.group()
    return None


def abi_minor_version(tag: str | None) -> tuple[int, int] | None:
    """Extract ``(major, minor)`` from an ABI tag like ``cp313`` or ``cpython-313``."""
    if not tag:
        return None
    match = re.search(r"\d+", tag)
    if not match:
        return None
    digits = match.group()
    if len(digits) < 2:
        return None
    return (int(digits[0]), int(digits[1:]))


def is_suppressed() -> bool:
    return bool(getattr(_roar_suppress, "active", False))


class SuppressTracking:
    """Context manager: file operations inside are not tracked."""

    def __enter__(self):
        _roar_suppress.active = True
        return self

    def __exit__(self, *_):
        _roar_suppress.active = False


def merge_working_dir(source_dir: str, target_dir: str) -> None:
    for entry in os.listdir(source_dir):
        src = os.path.join(source_dir, entry)
        dst = os.path.join(target_dir, entry)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except Exception:
            continue


def warn_runtime(message: str, *args: object) -> None:
    text = message % args if args else message
    try:
        from roar.core.logging import get_logger

        get_logger().warning(text)
        return
    except Exception:
        pass

    with contextlib.suppress(Exception):
        sys.stderr.write(text + "\n")
