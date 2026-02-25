from __future__ import annotations

from types import SimpleNamespace

from roar.services.execution.inject import sitecustomize


def test_prime_new_ray_nodes_primes_only_new_alive_nodes(monkeypatch) -> None:
    primed_nodes: list[str] = []
    fake_ray = SimpleNamespace(
        nodes=lambda: [
            {"NodeID": "node-head", "Alive": True},
            {"NodeID": "node-worker-new", "Alive": True},
            {"NodeID": "node-dead", "Alive": False},
        ]
    )

    monkeypatch.setattr(
        sitecustomize,
        "_prime_ray_node",
        lambda _ray_module, node_id: primed_nodes.append(node_id),
    )

    seen_nodes = {"node-head"}
    sitecustomize._prime_new_ray_nodes(fake_ray, seen_nodes)

    assert primed_nodes == ["node-worker-new"]
    assert seen_nodes == {"node-head", "node-worker-new"}
