"""Ray job that reproduces unbound background-thread native I/O attribution."""

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
    source_path = Path.cwd() / "native_background_thread_writer.c"
    library_path = Path.cwd() / "libnative_background_thread_writer.so"
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


@ray.remote(max_concurrency=1)
class BackgroundThreadNativeWriter:
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
        self._background_thread: threading.Thread | None = None
        self._background_meta: dict[str, str] = {}

    def launch_background_write(
        self, path: str, payload: str, native_delay_ms: int
    ) -> dict[str, str]:
        from roar.backends.ray import roar_worker

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        started = threading.Event()
        self._background_meta = {}

        def _run() -> None:
            python_thread_id = threading.get_native_id()
            native_thread_id = self._current_native_tid()
            self._background_meta = {
                "path": str(target),
                "background_thread_id": str(python_thread_id),
                "native_thread_id": str(native_thread_id),
                "pre_write_bound_task_id": roar_worker._bound_native_task_id_for_event(
                    os.getpid(),
                    python_thread_id,
                ),
            }
            started.set()

            rc = self._native_write_file(
                str(target).encode("utf-8"),
                payload.encode("utf-8"),
                native_delay_ms,
            )
            self._background_meta["returncode"] = str(rc)

        background_thread = threading.Thread(
            target=_run,
            name="native-background-writer",
        )
        self._background_thread = background_thread
        background_thread.start()
        if not started.wait(timeout=10):
            raise RuntimeError("background native writer thread did not start in time")

        ctx = ray.get_runtime_context()
        return {
            "path": str(target),
            "ld_preload": os.environ.get("LD_PRELOAD", ""),
            "trace_sock": os.environ.get("ROAR_PRELOAD_TRACE_SOCK", ""),
            "worker_id": _to_text(ctx.get_worker_id()),
            "task_id": _to_text(ctx.get_task_id()),
            "launch_thread_id": str(threading.get_native_id()),
            "background_thread_id": self._background_meta.get("background_thread_id", ""),
            "native_thread_id": self._background_meta.get("native_thread_id", ""),
            "pre_write_bound_task_id": self._background_meta.get("pre_write_bound_task_id", ""),
        }

    def block_on_next_task(self, marker_path: str, sleep_seconds: float) -> dict[str, str]:
        target = Path(marker_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("python marker\n", encoding="utf-8")
        time.sleep(sleep_seconds)

        ctx = ray.get_runtime_context()
        return {
            "path": str(target),
            "worker_id": _to_text(ctx.get_worker_id()),
            "task_id": _to_text(ctx.get_task_id()),
        }

    def wait_for_background(self) -> dict[str, str]:
        if self._background_thread is None:
            return {}
        self._background_thread.join(timeout=10)
        if self._background_thread.is_alive():
            raise RuntimeError("background native writer thread did not finish in time")
        return dict(self._background_meta)


def main() -> None:
    ray.init(address="auto")

    actor = BackgroundThreadNativeWriter.options(num_cpus=1).remote()
    native_path = str(Path.cwd() / "artifacts" / "native_background_thread_output.txt")
    marker_path = str(Path.cwd() / "artifacts" / "native_background_thread_marker.txt")

    launch_ref = actor.launch_background_write.remote(
        native_path,
        "background native thread\n",
        600,
    )
    block_ref = actor.block_on_next_task.remote(marker_path, 1.2)
    waited_ref = actor.wait_for_background.remote()

    launch, block, waited = ray.get([launch_ref, block_ref, waited_ref])
    print(json.dumps({"launch": launch, "block": block, "waited": waited}))


if __name__ == "__main__":
    main()
