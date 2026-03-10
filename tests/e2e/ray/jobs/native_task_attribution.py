"""Ray job that reproduces delayed native child I/O task attribution."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import time
from pathlib import Path

import ray

_HELPER_SOURCE = textwrap.dedent(
    r"""
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <unistd.h>

    int main(int argc, char **argv) {
        if (argc != 3) {
            fprintf(stderr, "usage: %s <path> <delay_ms>\n", argv[0]);
            return 2;
        }

        const char *path = argv[1];
        int delay_ms = atoi(argv[2]);
        if (delay_ms > 0) {
            usleep((useconds_t)delay_ms * 1000);
        }

        FILE *handle = fopen(path, "wb");
        if (handle == NULL) {
            perror("fopen");
            return 3;
        }

        const char *payload = "native child output\n";
        size_t payload_len = strlen(payload);
        if (fwrite(payload, 1, payload_len, handle) != payload_len) {
            perror("fwrite");
            fclose(handle);
            return 4;
        }

        if (fclose(handle) != 0) {
            perror("fclose");
            return 5;
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


@ray.remote(max_concurrency=1)
class NativeAttributionActor:
    def __init__(self) -> None:
        self._helper_path = self._build_helper()
        self._children: list[subprocess.Popen[bytes]] = []

    def _build_helper(self) -> str:
        source_path = Path.cwd() / "native_writer_helper.c"
        binary_path = Path.cwd() / "native_writer_helper"
        source_path.write_text(_HELPER_SOURCE, encoding="utf-8")
        subprocess.run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-o", str(binary_path), str(source_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return str(binary_path)

    def launch_delayed_native_write(self, path: str, delay_ms: int) -> dict[str, str]:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        child = subprocess.Popen(
            [self._helper_path, str(target), str(delay_ms)],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._children.append(child)

        ctx = ray.get_runtime_context()
        return {
            "path": str(target),
            "ld_preload": os.environ.get("LD_PRELOAD", ""),
            "trace_sock": os.environ.get("ROAR_PRELOAD_TRACE_SOCK", ""),
            "worker_id": _to_text(ctx.get_worker_id()),
            "task_id": _to_text(ctx.get_task_id()),
            "child_pid": str(child.pid),
        }

    def block_on_next_task(self, path: str, sleep_seconds: float) -> dict[str, str]:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("python marker\n", encoding="utf-8")
        time.sleep(sleep_seconds)

        ctx = ray.get_runtime_context()
        return {
            "path": str(target),
            "worker_id": _to_text(ctx.get_worker_id()),
            "task_id": _to_text(ctx.get_task_id()),
        }

    def wait_for_children(self) -> dict[str, object]:
        results: list[dict[str, object]] = []
        while self._children:
            child = self._children.pop(0)
            _, stderr = child.communicate(timeout=5)
            results.append(
                {
                    "pid": child.pid,
                    "returncode": child.returncode,
                    "stderr": stderr.decode("utf-8", errors="replace").strip(),
                }
            )
        return {"children": results}


def main() -> None:
    ray.init(address="auto")

    actor = NativeAttributionActor.options(num_cpus=1).remote()
    native_path = str(Path.cwd() / "artifacts" / "native_task_output.txt")
    marker_path = str(Path.cwd() / "artifacts" / "native_task_marker.txt")

    launch = ray.get(actor.launch_delayed_native_write.remote(native_path, 400))
    block = ray.get(actor.block_on_next_task.remote(marker_path, 1.0))
    waited = ray.get(actor.wait_for_children.remote())

    print(
        json.dumps(
            {
                "launch": launch,
                "block": block,
                "waited": waited,
            }
        )
    )


if __name__ == "__main__":
    main()
