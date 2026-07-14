from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from roar.execution.framework.contract import BackendConfigAdapter, ConfigurableKeySpec


class K8sBackendConfig(BaseModel):
    """Kubernetes backend configuration."""

    model_config = ConfigDict(
        strict=False,
        validate_assignment=True,
        extra="ignore",
        revalidate_instances="never",
    )

    enabled: bool = False
    tracer: str = "preload"
    # How pods obtain the roar runtime: "install" pip-installs
    # runtime_install_requirement at container start; "image" stages
    # per-ABI trees from runtime_image via an init container (hermetic,
    # no network at pod start).
    runtime_source: str = "install"
    runtime_image: str = ""
    runtime_install_requirement: str = ""
    # Opt-in roar-proxy S3 sidecar for clients the in-process hooks can't
    # see (aws CLI, s5cmd, Go/Java SDKs). Requires runtime_source="image"
    # (the sidecar runs the proxy binary from runtime_image).
    proxy_sidecar: bool = False
    proxy_upstream: str = ""
    cluster_glaas_url: str = ""
    bundle_dir: str = ""
    # Explicit mount-path -> object-store URI mapping for mounted storage
    # whose remote identity isn't visible in the pod spec (PVC-backed FUSE
    # CSI). TOML table, e.g. [k8s.mount_map] "/data" = "gs://bucket/prefix".
    mount_map: dict[str, str] = Field(default_factory=dict)
    wait_for_completion: bool = True
    wait_timeout_seconds: int = Field(default=30 * 60, ge=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)
    fragment_session_ttl_seconds: int = Field(default=86400, ge=60)


K8S_CONFIGURABLE_KEYS = {
    "k8s.enabled": ConfigurableKeySpec(
        value_type=bool,
        default=False,
        description="Enable automatic kubectl Job submit handling in roar run",
    ),
    "k8s.tracer": ConfigurableKeySpec(
        value_type=str,
        default="preload",
        description="Tracer backend used inside instrumented pods (preload|ptrace|auto)",
    ),
    "k8s.runtime_source": ConfigurableKeySpec(
        value_type=str,
        default="install",
        description=(
            "Runtime staging mode for pods: 'install' (pip at container start) or "
            "'image' (init container copies per-ABI trees from k8s.runtime_image)"
        ),
    ),
    "k8s.runtime_image": ConfigurableKeySpec(
        value_type=str,
        default="",
        description="roar-runtime OCI image used when k8s.runtime_source = 'image'",
    ),
    "k8s.runtime_install_requirement": ConfigurableKeySpec(
        value_type=str,
        default="",
        description=(
            "Pinned requirement or wheel URL installed inside pods to bootstrap the roar "
            "runtime; defaults to the submitter's pinned roar-cli version. Wheels must "
            "include packaged tracer binaries"
        ),
    ),
    "k8s.cluster_glaas_url": ConfigurableKeySpec(
        value_type=str,
        default="",
        description=(
            "Cluster-visible GLaaS URL injected into pods when it differs from the "
            "host-visible glaas.url (ROAR_CLUSTER_GLAAS_URL env always wins)"
        ),
    ),
    "k8s.proxy_sidecar": ConfigurableKeySpec(
        value_type=bool,
        default=False,
        description=(
            "Inject the roar-proxy S3 sidecar (native init container) to capture "
            "S3 traffic from non-Python clients; requires k8s.runtime_source='image'. "
            "In-process hooks remain the primary capture; user-set AWS_ENDPOINT_URL "
            "always wins over the injected proxy redirect"
        ),
    ),
    "k8s.proxy_upstream": ConfigurableKeySpec(
        value_type=str,
        default="",
        description=(
            "Upstream S3 endpoint the proxy sidecar forwards to (empty = real AWS S3); "
            "set for MinIO/LocalStack-style deployments"
        ),
    ),
    "k8s.bundle_dir": ConfigurableKeySpec(
        value_type=str,
        default="",
        description=(
            "In-pod directory (a mounted shared volume) where pods write "
            "roar-fragments-<pod>.json bundles when GLaaS streaming is unavailable; "
            "ingest later with `roar k8s ingest-bundles`"
        ),
    ),
    "k8s.wait_for_completion": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Wait for submitted Jobs to reach a terminal state before completing roar run",
    ),
    "k8s.wait_timeout_seconds": ConfigurableKeySpec(
        value_type=int,
        default=30 * 60,
        description="Maximum time to wait for a submitted Job to reach a terminal state",
    ),
    "k8s.poll_interval_seconds": ConfigurableKeySpec(
        value_type=float,
        default=5.0,
        description="Polling interval in seconds when waiting for Job completion",
    ),
    "k8s.fragment_session_ttl_seconds": ConfigurableKeySpec(
        value_type=int,
        default=86400,
        description="TTL requested when pre-registering the GLaaS fragment session for a Job",
    ),
}

K8S_INIT_TEMPLATE = """\
[k8s]
# Enable kubectl Job submit recognition in roar run
enabled = false
# Tracer backend used inside instrumented pods
tracer = "preload"
# Optional pinned requirement or wheel URL installed inside pods
# For roar-cli, use a packaged wheel or index source that includes bundled tracer binaries
runtime_install_requirement = ""
# Cluster-visible GLaaS URL when pods cannot reach the host-visible glaas.url
cluster_glaas_url = ""
# Wait for submitted Jobs to finish so lineage can be reconstituted immediately
wait_for_completion = true

# Optional: map mounted storage paths to their object-store URIs when the
# pod spec can't reveal them (PVC-backed FUSE CSI drivers). Inline CSI
# volumes (GCS FUSE, Mountpoint-for-S3) are detected automatically.
# [k8s.mount_map]
# "/data" = "gs://my-bucket/datasets"
"""


def normalize_k8s_backend_config(section: Mapping[str, Any] | None) -> dict[str, Any]:
    return K8sBackendConfig.model_validate(dict(section or {})).model_dump()


def load_k8s_backend_config(start_dir: str | None = None) -> dict[str, Any]:
    try:
        from roar.integrations.config import load_config

        config = load_config(start_dir=start_dir)
    except Exception:
        return dict(K8S_BACKEND_CONFIG.default_values)

    section = config.get("k8s", {})
    if not isinstance(section, Mapping):
        return dict(K8S_BACKEND_CONFIG.default_values)
    return normalize_k8s_backend_config(section)


K8S_BACKEND_CONFIG = BackendConfigAdapter(
    section_name="k8s",
    default_values=K8sBackendConfig().model_dump(),
    configurable_keys=K8S_CONFIGURABLE_KEYS,
    init_template=K8S_INIT_TEMPLATE,
    normalize_section=normalize_k8s_backend_config,
)


__all__ = [
    "K8S_BACKEND_CONFIG",
    "K8S_CONFIGURABLE_KEYS",
    "K8S_INIT_TEMPLATE",
    "K8sBackendConfig",
    "load_k8s_backend_config",
    "normalize_k8s_backend_config",
]
