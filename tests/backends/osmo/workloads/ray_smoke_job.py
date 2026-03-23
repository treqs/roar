from __future__ import annotations

import json
import os
import socket
import time

import ray


@ray.remote(num_cpus=1)
def square(value: int) -> dict[str, object]:
    time.sleep(2)
    return {
        "value": value,
        "square": value * value,
        "hostname": socket.gethostname(),
    }


def _wait_for_cluster() -> list[dict[str, object]]:
    expected_nodes = int(os.environ.get("ROAR_EXPECTED_RAY_NODES", "1"))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        nodes = [node for node in ray.nodes() if node.get("Alive")]
        if len(nodes) >= expected_nodes:
            return nodes
        time.sleep(2)
    raise SystemExit(f"expected at least {expected_nodes} live Ray nodes")


def main() -> None:
    ray.init(address="auto", ignore_reinit_error=True, logging_level="ERROR")
    _wait_for_cluster()

    results = ray.get([square.remote(value) for value in range(6)])
    hostnames = sorted({str(result["hostname"]) for result in results})
    payload = {
        "host_count": len(hostnames),
        "hosts": hostnames,
        "node_count": len([node for node in ray.nodes() if node.get("Alive")]),
        "total": sum(int(result["square"]) for result in results),
    }

    if payload["total"] != 55:
        raise SystemExit(f"unexpected total: {payload['total']}")
    if payload["node_count"] < int(os.environ.get("ROAR_EXPECTED_RAY_NODES", "1")):
        raise SystemExit(f"expected more live Ray nodes, got {payload['node_count']}")

    print(f"ROAR_OSMO_RAY_OK {json.dumps(payload, sort_keys=True)}", flush=True)
    ray.shutdown()


if __name__ == "__main__":
    main()
