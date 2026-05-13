from __future__ import annotations

from pathlib import Path

from roar.backends.ray.env_contract import ROAR_NO_TELEMETRY_ENV
from roar.backends.ray.submit_context import (
    build_submit_instrumentation_context,
    build_submit_source_environ,
    derive_submit_proxy_port,
)


def test_build_submit_instrumentation_context_resolves_cluster_overrides(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "roar.db").write_text("", encoding="utf-8")

    context = build_submit_instrumentation_context(
        {
            "AWS_ENDPOINT_URL": "http://localhost:9000",
            "ROAR_CLUSTER_GLAAS_URL": "http://host.docker.internal:3001",
            "ROAR_CLUSTER_AWS_ENDPOINT_URL": "http://minio:9000",
        },
        cwd=str(tmp_path),
        host_glaas_url="http://localhost:3001",
        job_id="job-alpha",
    )

    assert context.job_id == "job-alpha"
    assert context.proxy_port == derive_submit_proxy_port("job-alpha")
    assert context.project_dir == str(tmp_path)
    assert context.endpoints.host_glaas_url == "http://localhost:3001"
    assert context.endpoints.cluster_glaas_url == "http://host.docker.internal:3001"
    assert context.endpoints.host_upstream_s3_endpoint == "http://localhost:9000"
    assert context.endpoints.cluster_upstream_s3_endpoint == "http://minio:9000"


def test_build_submit_instrumentation_context_ignores_missing_roar_db(tmp_path: Path) -> None:
    context = build_submit_instrumentation_context(
        {},
        cwd=str(tmp_path),
        host_glaas_url=None,
        job_id="job-no-db",
    )

    assert context.project_dir is None
    assert context.endpoints.host_glaas_url is None
    assert context.endpoints.cluster_glaas_url is None


def test_build_submit_source_environ_uses_cluster_visible_endpoints(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "roar.db").write_text("", encoding="utf-8")

    context = build_submit_instrumentation_context(
        {
            "ROAR_UPSTREAM_S3_ENDPOINT": "http://localhost:9000",
            "ROAR_CLUSTER_AWS_ENDPOINT_URL": "http://minio:9000",
            "ROAR_CLUSTER_GLAAS_URL": "http://host.docker.internal:3001",
        },
        cwd=str(tmp_path),
        host_glaas_url="http://localhost:3001",
        job_id="job-source",
    )

    source = build_submit_source_environ(context)

    assert source["ROAR_JOB_INSTRUMENTED"] == "1"
    assert source[ROAR_NO_TELEMETRY_ENV] == "1"
    assert source["ROAR_RAY_NODE_AGENTS"] == "1"
    assert source["ROAR_PROJECT_DIR"] == str(tmp_path)
    assert source["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://minio:9000"
    assert source["ROAR_CLUSTER_GLAAS_URL"] == "http://host.docker.internal:3001"
    assert source["ROAR_PROXY_PORT"] == str(derive_submit_proxy_port("job-source"))
    assert source["AWS_ENDPOINT_URL"] == f"http://127.0.0.1:{source['ROAR_PROXY_PORT']}"


def test_derive_submit_proxy_port_is_stable_and_job_scoped() -> None:
    port_a = derive_submit_proxy_port("job-alpha")
    port_b = derive_submit_proxy_port("job-beta")

    assert 20000 <= port_a < 40000
    assert 20000 <= port_b < 40000
    assert port_a == derive_submit_proxy_port("job-alpha")
    assert port_a != port_b
