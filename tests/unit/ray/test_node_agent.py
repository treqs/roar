from __future__ import annotations

import socket

import pytest

ray = pytest.importorskip("ray")

from roar.ray.node_agent import RoarNodeAgent  # noqa: E402


@pytest.fixture
def ray_runtime() -> None:
    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        namespace="roar-test-node-agent",
        ignore_reinit_error=True,
        include_dashboard=False,
        num_cpus=1,
        log_to_driver=False,
    )
    try:
        yield
    finally:
        ray.shutdown()


def test_node_agent_starts_proxy_and_collects_logs(ray_runtime, tmp_path) -> None:
    agent = RoarNodeAgent.remote(job_id="job-test", log_dir=str(tmp_path))

    try:
        port = ray.get(agent.get_proxy_port.remote(), timeout=15)
        assert isinstance(port, int)
        assert port > 0

        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass

        payload = ray.get(agent.collect_logs.remote(), timeout=5)
        assert isinstance(payload, dict)
        assert payload.get("proxy_port") == port
        log_lines = payload.get("proxy_log_lines") or []
        assert isinstance(log_lines, list)
        assert any("ROAR_PROXY_READY" in line for line in log_lines)
    finally:
        ray.get(agent.shutdown.remote(), timeout=5)
        ray.kill(agent)
