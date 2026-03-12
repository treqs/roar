from __future__ import annotations

import contextlib
import json
import os
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ray = pytest.importorskip("ray")

from roar.backends.ray.node_agent import RoarNodeAgent, _local_proxy_port  # noqa: E402
from roar.services.execution.cluster_bridge import (  # noqa: E402
    LocalProxyClusterBridge,
    proxy_claim_path,
)


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


def test_node_agent_starts_proxy_and_collects_logs(ray_runtime) -> None:
    agent = RoarNodeAgent.remote(job_id="job-test")

    try:
        try:
            port = ray.get(agent.get_proxy_port.remote(), timeout=15)
        except Exception as exc:
            pytest.skip(f"node agent proxy did not become ready: {exc}")
        if not isinstance(port, int) or port <= 0:
            pytest.skip(f"node agent proxy was not started (port={port!r})")

        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass

        payload = ray.get(agent.collect_logs.remote(), timeout=5)
        assert isinstance(payload, dict)
        assert payload.get("proxy_port") == port
        log_lines = payload.get("proxy_log_lines") or []
        assert isinstance(log_lines, list)
        assert any("ROAR_PROXY_READY" in line for line in log_lines)
    finally:
        with contextlib.suppress(Exception):
            ray.get(agent.shutdown.remote(), timeout=5)
        with contextlib.suppress(Exception):
            ray.kill(agent)


def test_local_proxy_port_prefers_roar_proxy_port_env(monkeypatch) -> None:
    monkeypatch.setenv("ROAR_PROXY_PORT", "24567")
    assert _local_proxy_port() == 24567


def test_local_proxy_port_falls_back_for_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("ROAR_PROXY_PORT", "invalid")
    assert _local_proxy_port() == 19191


def test_cluster_bridge_refuses_reuse_when_existing_listener_claim_upstream_differs(
    monkeypatch, tmp_path: Path
) -> None:
    port = 24567
    claim_path = proxy_claim_path(port, {"ROAR_PROXY_CLAIM_DIR": str(tmp_path)})
    claim_path.write_text(
        json.dumps(
            {
                "job_id": "job-shared",
                "upstream": "http://minio-b:9000",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.can_connect_to_local_proxy",
        lambda _: True,
    )
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.tracer_backends.find_proxy_binary",
        lambda _: "/fake/roar-proxy",
    )
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected Popen")),
    )

    bridge = LocalProxyClusterBridge(
        Path("/tmp/fake-package"),
        env={"ROAR_PROXY_CLAIM_DIR": str(tmp_path)},
    )
    handle = bridge.start(
        job_id="job-shared",
        port=port,
        upstream_url="http://minio-a:9000",
    )

    assert handle is None
    assert claim_path.exists()


def test_cluster_bridge_reuses_existing_listener_when_claim_matches(
    monkeypatch, tmp_path: Path
) -> None:
    port = 24567
    claim_path = proxy_claim_path(port, {"ROAR_PROXY_CLAIM_DIR": str(tmp_path)})
    claim_path.write_text(
        json.dumps(
            {
                "job_id": "job-shared",
                "upstream": "http://minio-a:9000",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.can_connect_to_local_proxy",
        lambda _: True,
    )
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.tracer_backends.find_proxy_binary",
        lambda _: "/fake/roar-proxy",
    )
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected Popen")),
    )

    bridge = LocalProxyClusterBridge(
        Path("/tmp/fake-package"),
        env={"ROAR_PROXY_CLAIM_DIR": str(tmp_path)},
    )
    handle = bridge.start(
        job_id="job-shared",
        port=port,
        upstream_url="http://minio-a:9000",
    )

    assert handle is not None
    assert handle.port == port
    assert handle.process is None
    assert claim_path.exists()


def test_cluster_bridge_writes_and_clears_proxy_claim_for_owned_process(
    monkeypatch, tmp_path: Path
) -> None:
    port = 24567
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.can_connect_to_local_proxy",
        lambda _: False,
    )
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.tracer_backends.find_proxy_binary",
        lambda _: "/fake/roar-proxy",
    )

    process = MagicMock()
    process.pid = 12345
    process.poll.return_value = None
    process.stdout = iter(["ROAR_PROXY_READY port=24567\n"])
    monkeypatch.setattr(
        "roar.services.execution.cluster_bridge.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    bridge = LocalProxyClusterBridge(
        Path("/tmp/fake-package"),
        env={"ROAR_PROXY_CLAIM_DIR": str(tmp_path)},
    )
    handle = bridge.start(
        job_id="job-owned",
        port=port,
        upstream_url="http://minio-a:9000",
    )

    claim_path = proxy_claim_path(port, {"ROAR_PROXY_CLAIM_DIR": str(tmp_path)})
    assert handle is not None
    assert handle.port == port
    assert claim_path.exists()
    assert json.loads(claim_path.read_text(encoding="utf-8")) == {
        "job_id": "job-owned",
        "upstream": "http://minio-a:9000",
        "pid": 12345,
    }

    bridge.stop(handle)

    assert not claim_path.exists()
