"""Ray job that forces concurrent same-process native writes on one actor worker."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import ray

_LIBRARY_SOURCE = textwrap.dedent(
    r"""
    #include <fcntl.h>
    #include <sys/syscall.h>
    #include <string.h>
    #include <unistd.h>

    int native_write_file(const char *path, const char *payload, int delay_ms) {
        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
        if (fd < 0) {
            return 2;
        }

        if (delay_ms > 0) {
            usleep((useconds_t)delay_ms * 1000);
        }

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

    int current_native_tid(void) {
        return (int)syscall(SYS_gettid);
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
    source_path = Path.cwd() / "native_thread_writer.c"
    library_path = Path.cwd() / "libnative_thread_writer.so"
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


@ray.remote(max_concurrency=2)
class ThreadedNativeWriter:
    def __init__(self) -> None:
        library = ctypes.CDLL(str(_build_library()))
        native_write_file = library.native_write_file
        native_write_file.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        native_write_file.restype = ctypes.c_int

        current_native_tid = library.current_native_tid
        current_native_tid.argtypes = []
        current_native_tid.restype = ctypes.c_int

        self._native_write_file = native_write_file
        self._current_native_tid = current_native_tid
        self._start_barrier = threading.Barrier(2)
        self._finish_barrier = threading.Barrier(2)

    def write(
        self, path: str, payload: str, native_delay_ms: int, return_delay_ms: int
    ) -> dict[str, str]:
        from roar.ray import roar_worker

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        python_thread_id = threading.get_native_id()
        native_thread_id = self._current_native_tid()
        pre_write_bound_task_id = roar_worker._bound_native_task_id_for_event(
            os.getpid(),
            python_thread_id,
        )

        self._start_barrier.wait(timeout=30)
        rc = self._native_write_file(
            str(target).encode("utf-8"),
            payload.encode("utf-8"),
            native_delay_ms,
        )
        if rc != 0:
            raise RuntimeError(f"native_write_file failed with exit code {rc}")

        self._finish_barrier.wait(timeout=30)
        if return_delay_ms > 0:
            time.sleep(return_delay_ms / 1000.0)

        ctx = ray.get_runtime_context()
        return {
            "path": str(target),
            "ld_preload": os.environ.get("LD_PRELOAD", ""),
            "trace_sock": os.environ.get("ROAR_PRELOAD_TRACE_SOCK", ""),
            "worker_id": _to_text(ctx.get_worker_id()),
            "task_id": _to_text(ctx.get_task_id()),
            "thread_id": str(python_thread_id),
            "native_thread_id": str(native_thread_id),
            "pre_write_bound_task_id": pre_write_bound_task_id,
        }


def main() -> None:
    ray.init(address="auto")

    writer = ThreadedNativeWriter.remote()
    fast_ref = writer.write.remote(
        str(Path.cwd() / "artifacts" / "native_thread_fast.txt"),
        "fast native thread\n",
        0,
        0,
    )
    slow_ref = writer.write.remote(
        str(Path.cwd() / "artifacts" / "native_thread_slow.txt"),
        "slow native thread\n",
        0,
        250,
    )

    fast, slow = ray.get([fast_ref, slow_ref])
    print(json.dumps({"fast": fast, "slow": slow}))


if __name__ == "__main__":
    main()
