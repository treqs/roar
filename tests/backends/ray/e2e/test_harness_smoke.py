"""Smoke tests for the Docker-based Ray test harness."""

from __future__ import annotations

import boto3
import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy


def test_cluster_is_reachable(ray_connection) -> None:
    @ray.remote
    def ping() -> str:
        return "pong"

    assert ray.get(ping.remote()) == "pong"


def test_cluster_has_multiple_nodes(ray_connection) -> None:
    alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
    assert len(alive_nodes) >= 3


def test_tasks_run_on_workers(ray_connection) -> None:
    alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
    assert len(alive_nodes) >= 2
    target_node_ids = [alive_nodes[0]["NodeID"], alive_nodes[1]["NodeID"]]

    @ray.remote
    def current_node_id() -> str:
        return ray.get_runtime_context().get_node_id()

    refs = [
        current_node_id.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                node_id=target_node_ids[i % 2],
                soft=False,
            )
        ).remote()
        for i in range(10)
    ]
    node_ids = ray.get(refs)
    assert len(set(node_ids)) >= 2


def test_minio_is_accessible(ray_cluster: dict[str, str]) -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=ray_cluster["minio_endpoint"],
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )

    buckets = [bucket["Name"] for bucket in s3.list_buckets().get("Buckets", [])]
    assert "test-bucket" in buckets


def test_worker_local_filesystem_accessible(ray_connection) -> None:
    @ray.remote
    def write_and_read_local_file() -> str:
        path = "/tmp/roar_smoke_test.txt"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("local-data-ok")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    assert ray.get(write_and_read_local_file.remote()) == "local-data-ok"
