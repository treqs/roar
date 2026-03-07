"""Probe: verify the proxy endpoint injected via AWS_ENDPOINT_URL is reachable from workers.

Each Ray task reports whether it can connect to the URL in AWS_ENDPOINT_URL.
This exposes the worker-proxy routing bug: if AWS_ENDPOINT_URL is set to
http://127.0.0.1:19191 (the host machine's proxy), workers running in separate
processes/containers cannot reach it and the probe returns reachable=False.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.parse

import ray


@ray.remote
def check_proxy_endpoint() -> dict[str, object]:
    """Return connectivity info for the proxy endpoint this worker sees."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    node_id = ray.get_runtime_context().get_node_id().hex()

    if not endpoint:
        return {"endpoint": None, "reachable": False, "error": "AWS_ENDPOINT_URL not set", "node_id": node_id}

    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or 80

    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        return {"endpoint": endpoint, "reachable": True, "error": None, "node_id": node_id}
    except OSError as exc:
        return {"endpoint": endpoint, "reachable": False, "error": str(exc), "node_id": node_id}


def main() -> None:
    ray.init(address="auto")
    try:
        # Scatter across workers — use enough tasks to hit all nodes.
        futures = [check_proxy_endpoint.remote() for _ in range(6)]
        results = ray.get(futures)
        print(json.dumps({"results": results}))
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
