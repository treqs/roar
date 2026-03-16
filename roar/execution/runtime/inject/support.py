from __future__ import annotations

import contextlib
import os
import shutil
import sys
import threading

_roar_suppress = threading.local()


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
