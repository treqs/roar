"""Ray submit-side instrumentation context construction."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import blake2s

from roar.backends.ray.env_contract import (
    ROAR_CLUSTER_AWS_ENDPOINT_URL_ENV,
    ROAR_CLUSTER_GLAAS_URL_ENV,
    resolve_cluster_glaas_url,
)
from roar.ray.proxy_config import DEFAULT_LOCAL_PROXY_PORT, local_proxy_endpoint

ROAR_JOB_INSTRUMENTED_ENV_VAR = "ROAR_JOB_INSTRUMENTED"
_ROAR_PROXY_PORT_BASE = 20000
_ROAR_PROXY_PORT_RANGE = 20000


@dataclass(frozen=True)
class RayClusterEndpoints:
    host_glaas_url: str | None
    cluster_glaas_url: str | None
    host_upstream_s3_endpoint: str | None
    cluster_upstream_s3_endpoint: str | None


@dataclass(frozen=True)
class RayInstrumentationContext:
    job_id: str
    proxy_port: int
    project_dir: str | None
    endpoints: RayClusterEndpoints


def build_submit_instrumentation_context(
    environ: Mapping[str, str],
    *,
    cwd: str,
    host_glaas_url: str | None,
    job_id: str | None = None,
) -> RayInstrumentationContext:
    resolved_job_id = job_id or generate_job_id()
    return RayInstrumentationContext(
        job_id=resolved_job_id,
        proxy_port=derive_submit_proxy_port(resolved_job_id),
        project_dir=resolve_project_dir(environ, cwd=cwd),
        endpoints=resolve_submit_cluster_endpoints(environ, host_glaas_url=host_glaas_url),
    )


def build_submit_source_environ(context: RayInstrumentationContext) -> dict[str, str]:
    source_environ = {
        ROAR_JOB_INSTRUMENTED_ENV_VAR: "1",
        "ROAR_RAY_NODE_AGENTS": "1",
        "ROAR_PROXY_PORT": str(context.proxy_port),
        "AWS_ENDPOINT_URL": local_proxy_endpoint(context.proxy_port),
    }
    if context.project_dir:
        source_environ["ROAR_PROJECT_DIR"] = context.project_dir
    if context.endpoints.cluster_upstream_s3_endpoint:
        source_environ["ROAR_UPSTREAM_S3_ENDPOINT"] = context.endpoints.cluster_upstream_s3_endpoint
    if context.endpoints.cluster_glaas_url:
        source_environ[ROAR_CLUSTER_GLAAS_URL_ENV] = context.endpoints.cluster_glaas_url
    return source_environ


def generate_job_id() -> str:
    # Stable job_id shared by driver + workers for node agent name resolution.
    return uuid.uuid4().hex[:8]


def derive_submit_proxy_port(job_id: str) -> int:
    text = str(job_id).strip()
    if not text:
        return DEFAULT_LOCAL_PROXY_PORT

    digest = blake2s(text.encode("utf-8"), digest_size=2).digest()
    value = int.from_bytes(digest, byteorder="big", signed=False)
    return _ROAR_PROXY_PORT_BASE + (value % _ROAR_PROXY_PORT_RANGE)


def resolve_project_dir(environ: Mapping[str, str], *, cwd: str) -> str | None:
    # The Ray job CWD is the extracted working_dir, not the local project root.
    candidate = str(environ.get("ROAR_PROJECT_DIR") or cwd).strip()
    if candidate and os.path.isfile(os.path.join(candidate, ".roar", "roar.db")):
        return candidate
    return None


def resolve_submit_cluster_endpoints(
    environ: Mapping[str, str],
    *,
    host_glaas_url: str | None,
) -> RayClusterEndpoints:
    host_upstream_s3_endpoint = resolve_host_upstream_s3_endpoint(environ)
    return RayClusterEndpoints(
        host_glaas_url=host_glaas_url,
        cluster_glaas_url=resolve_submit_cluster_glaas_url(environ, host_glaas_url=host_glaas_url),
        host_upstream_s3_endpoint=host_upstream_s3_endpoint,
        cluster_upstream_s3_endpoint=resolve_cluster_upstream_s3_endpoint(
            environ,
            host_endpoint=host_upstream_s3_endpoint,
        ),
    )


def resolve_host_upstream_s3_endpoint(environ: Mapping[str, str]) -> str | None:
    text = str(
        environ.get("ROAR_UPSTREAM_S3_ENDPOINT") or environ.get("AWS_ENDPOINT_URL") or ""
    ).strip()
    return text or None


def resolve_submit_cluster_glaas_url(
    environ: Mapping[str, str],
    *,
    host_glaas_url: str | None,
) -> str | None:
    override = resolve_cluster_glaas_url(environ)
    if override:
        return override
    if not host_glaas_url:
        return None
    return str(host_glaas_url)


def resolve_cluster_upstream_s3_endpoint(
    environ: Mapping[str, str],
    *,
    host_endpoint: str | None,
) -> str | None:
    override = str(environ.get(ROAR_CLUSTER_AWS_ENDPOINT_URL_ENV) or "").strip()
    if override:
        return override
    if not host_endpoint:
        return None
    return str(host_endpoint)
