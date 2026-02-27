from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from roar.ray import worker


class _FakeRemoteMethod:
    def __init__(self, value):
        self._value = value

    def remote(self):
        return self._value


class _FakeAgent:
    def __init__(self, port: int):
        self.get_proxy_port = _FakeRemoteMethod(port)


class _FakeRay:
    def __init__(self, port: int):
        self._agent = _FakeAgent(port)
        self.get_actor_name: str | None = None
        self.get_actor_namespace: str | None = None

    def get_runtime_context(self):
        return SimpleNamespace(get_node_id=lambda: "node-12345678")

    def get_actor(self, name: str, namespace: str | None = None):
        self.get_actor_name = name
        self.get_actor_namespace = namespace
        return self._agent

    def get(self, value, timeout: int | None = None):
        del timeout
        return value


def test_configure_local_proxy_endpoint_from_node_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ray = _FakeRay(port=9012)
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    worker._configure_local_proxy_endpoint()

    assert fake_ray.get_actor_namespace == "roar"
    assert fake_ray.get_actor_name == "roar-node-agent-job-123-node-123"
    assert os.environ.get("AWS_ENDPOINT_URL") == "http://127.0.0.1:9012"


def test_configure_local_proxy_endpoint_preserves_existing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://existing-endpoint")
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")

    worker._configure_local_proxy_endpoint()

    assert os.environ.get("AWS_ENDPOINT_URL") == "http://existing-endpoint"
