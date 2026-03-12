"""Shared environment contract for Ray submit, driver, and worker bootstrap."""

from __future__ import annotations

from collections.abc import Mapping

ROAR_CLUSTER_GLAAS_URL_ENV = "ROAR_CLUSTER_GLAAS_URL"
ROAR_CLUSTER_AWS_ENDPOINT_URL_ENV = "ROAR_CLUSTER_AWS_ENDPOINT_URL"

_WORKER_PROPAGATED_ENV_KEYS = (
    "AWS_ENDPOINT_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "ROAR_PROXY_PORT",
    "ROAR_UPSTREAM_S3_ENDPOINT",
    "ROAR_PROJECT_DIR",
    "ROAR_JOB_INSTRUMENTED",
    "ROAR_RAY_NODE_AGENTS",
)
_FRAGMENT_ENV_KEYS = ("ROAR_SESSION_ID", "ROAR_FRAGMENT_TOKEN")


def resolve_cluster_glaas_url(environ: Mapping[str, str]) -> str | None:
    text = str(environ.get(ROAR_CLUSTER_GLAAS_URL_ENV) or environ.get("GLAAS_URL") or "").strip()
    return text or None


def merge_worker_bootstrap_env(
    existing_env_vars: Mapping[str, str] | None,
    source_environ: Mapping[str, str],
    *,
    job_id: str,
    driver_job_uid: str | None = None,
    overwrite_existing: bool = False,
) -> dict[str, str]:
    env_vars = dict(existing_env_vars or {})
    env_vars["ROAR_JOB_ID"] = job_id

    if driver_job_uid is not None:
        env_vars["ROAR_DRIVER_JOB_UID"] = str(driver_job_uid)

    for key in _WORKER_PROPAGATED_ENV_KEYS:
        _merge_env_value(
            env_vars, key, source_environ.get(key), overwrite_existing=overwrite_existing
        )

    cluster_glaas_url = resolve_cluster_glaas_url(source_environ)
    if cluster_glaas_url:
        _merge_env_value(
            env_vars,
            "GLAAS_URL",
            cluster_glaas_url,
            overwrite_existing=overwrite_existing,
        )

    for key in _FRAGMENT_ENV_KEYS:
        _merge_env_value(
            env_vars, key, source_environ.get(key), overwrite_existing=overwrite_existing
        )

    return env_vars


def _merge_env_value(
    env_vars: dict[str, str],
    key: str,
    value: object,
    *,
    overwrite_existing: bool,
) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if overwrite_existing:
        env_vars[key] = text
        return
    env_vars.setdefault(key, text)
