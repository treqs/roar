"""Ray job that performs in-process native I/O through a compiled shared library."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import textwrap
from pathlib import Path

import ray

_LIBRARY_SOURCE = textwrap.dedent(
    r"""
    #include <fcntl.h>
    #include <string.h>
    #include <unistd.h>

    int native_write_file(const char *path) {
        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
        if (fd < 0) {
            return 2;
        }

        const char *payload = "native library output\n";
        size_t payload_len = strlen(payload);
        if (write(fd, payload, payload_len) != (ssize_t)payload_len) {
            close(fd);
            return 3;
        }

        if (close(fd) != 0) {
            return 4;
        }

        return 0;
    }
    """
).strip()


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _build_library() -> Path:
    source_path = Path.cwd() / "native_writer_library.c"
    library_path = Path.cwd() / "libnative_writer.so"
    source_path.write_text(_LIBRARY_SOURCE, encoding="utf-8")
    subprocess.run(
        [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-o",
            str(library_path),
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library_path


@ray.remote
def write_via_native_library(path: str) -> dict[str, str]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    library = ctypes.CDLL(str(_build_library()))
    native_write_file = library.native_write_file
    native_write_file.argtypes = [ctypes.c_char_p]
    native_write_file.restype = ctypes.c_int

    rc = native_write_file(str(target).encode("utf-8"))
    if rc != 0:
        raise RuntimeError(f"native_write_file failed with exit code {rc}")

    ctx = ray.get_runtime_context()
    return {
        "path": str(target),
        "ld_preload": os.environ.get("LD_PRELOAD", ""),
        "trace_sock": os.environ.get("ROAR_PRELOAD_TRACE_SOCK", ""),
        "worker_id": _to_text(ctx.get_worker_id()),
        "task_id": _to_text(ctx.get_task_id()),
    }


def main() -> None:
    ray.init(address="auto")
    payload = ray.get(
        write_via_native_library.remote(str(Path.cwd() / "artifacts" / "native_library_output.txt"))
    )
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
