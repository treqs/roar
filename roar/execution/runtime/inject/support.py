from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import threading
from collections.abc import Callable
from typing import Protocol

_roar_suppress = threading.local()


class _BackendDispatchController(Protocol):
    """The slice of ``RuntimeImportController`` the runtime gate drives."""

    def disable_backend_dispatch(self) -> None: ...
    def enable_backend_dispatch(self) -> None: ...
    def initialize_selected_backend(self) -> None: ...


def apply_runtime_gate(
    controller: _BackendDispatchController,
    *,
    matched: bool,
    repair: Callable[[], bool],
    on_degrade: Callable[[], None],
) -> None:
    """Decide backend dispatch for a traced process based on ABI match.

    - ``matched`` (bundled/co-installed deps are the right ABI): enable backends.
    - mismatch: disable dispatch **first** — so the repair's own imports can't
      trigger a wrong-ABI backend load (the original cross-Python crash) — then
      attempt ``repair``; re-enable and initialize **only** if repair made
      ABI-matched deps reachable, otherwise ``on_degrade``.

    Extracted from ``sitecustomize`` so the ordering (disable-before-repair,
    enable-only-on-success) is unit-testable without importing the module body.
    """
    if matched:
        controller.initialize_selected_backend()
        return
    controller.disable_backend_dispatch()
    if repair():
        controller.enable_backend_dispatch()
        controller.initialize_selected_backend()
    else:
        on_degrade()


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


def matching_compiled_pydantic_core(sys_path: list[str], expected_soabi: str) -> bool:
    """Return True if a pydantic_core/_pydantic_core.<soabi>.so exists on ``sys_path``.

    ``expected_soabi`` is the long-form CPython SOABI substring (e.g.
    ``cpython-313``) — typically built from the running interpreter's version
    tuple. Used as the gate primitive in ``sitecustomize.py``: if a matching
    compiled pydantic_core is reachable (either in roar's bundled tree or in
    a lazy-installed runtime tree on ``ROAR_RUNTIME_PYTHONPATH``), backend
    dispatch can safely fire.
    """
    for entry in sys_path:
        if not entry:
            continue
        pdc_dir = os.path.join(entry, "pydantic_core")
        if not os.path.isdir(pdc_dir):
            continue
        try:
            filenames = os.listdir(pdc_dir)
        except OSError:
            continue
        for filename in filenames:
            if expected_soabi in filename and filename.endswith(".so"):
                return True
    return False


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
