from __future__ import annotations

from roar.backends.ray.env_contract import (
    ROAR_CLUSTER_GLAAS_URL_ENV,
    ROAR_NO_TELEMETRY_ENV,
    merge_worker_bootstrap_env,
    resolve_cluster_glaas_url,
)


def test_resolve_cluster_glaas_url_prefers_cluster_override() -> None:
    env = {
        "GLAAS_URL": "http://localhost:3001",
        ROAR_CLUSTER_GLAAS_URL_ENV: "http://host.docker.internal:3001",
    }

    assert resolve_cluster_glaas_url(env) == "http://host.docker.internal:3001"


def test_merge_worker_bootstrap_env_preserves_existing_values_by_default() -> None:
    env_vars = merge_worker_bootstrap_env(
        {
            "USER_KEY": "value",
            "GLAAS_URL": "http://user-configured:3001",
            "AWS_ENDPOINT_URL": "http://user-endpoint:9000",
        },
        {
            "AWS_ENDPOINT_URL": "http://127.0.0.1:24567",
            "ROAR_PROXY_PORT": "24567",
            ROAR_CLUSTER_GLAAS_URL_ENV: "http://cluster-glaas:3001",
            "ROAR_SESSION_ID": "session-123",
        },
        job_id="job-123",
        driver_job_uid="driver-123",
    )

    assert env_vars["USER_KEY"] == "value"
    assert env_vars["ROAR_JOB_ID"] == "job-123"
    assert env_vars["ROAR_DRIVER_JOB_UID"] == "driver-123"
    assert env_vars["GLAAS_URL"] == "http://user-configured:3001"
    assert env_vars["AWS_ENDPOINT_URL"] == "http://user-endpoint:9000"
    assert env_vars["ROAR_PROXY_PORT"] == "24567"
    assert env_vars["ROAR_SESSION_ID"] == "session-123"
    assert env_vars[ROAR_NO_TELEMETRY_ENV] == "1"


def test_merge_worker_bootstrap_env_can_overwrite_existing_values() -> None:
    env_vars = merge_worker_bootstrap_env(
        {
            "GLAAS_URL": "http://user-configured:3001",
            "AWS_ENDPOINT_URL": "http://user-endpoint:9000",
        },
        {
            "AWS_ENDPOINT_URL": "http://127.0.0.1:24567",
            "ROAR_PROXY_PORT": "24567",
            ROAR_CLUSTER_GLAAS_URL_ENV: "http://cluster-glaas:3001",
        },
        job_id="job-456",
        overwrite_existing=True,
    )

    assert env_vars["ROAR_JOB_ID"] == "job-456"
    assert env_vars["GLAAS_URL"] == "http://cluster-glaas:3001"
    assert env_vars["AWS_ENDPOINT_URL"] == "http://127.0.0.1:24567"
    assert env_vars["ROAR_PROXY_PORT"] == "24567"
    assert env_vars[ROAR_NO_TELEMETRY_ENV] == "1"


def test_merge_worker_bootstrap_env_forces_telemetry_suppression() -> None:
    env_vars = merge_worker_bootstrap_env(
        {
            "USER_KEY": "value",
            ROAR_NO_TELEMETRY_ENV: "0",
        },
        {},
        job_id="job-telemetry",
    )

    assert env_vars["USER_KEY"] == "value"
    assert env_vars[ROAR_NO_TELEMETRY_ENV] == "1"
