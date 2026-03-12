from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from roar.execution.framework.contract import BackendConfigAdapter, ConfigurableKeySpec


class RayBackendConfig(BaseModel):
    """Ray backend configuration."""

    model_config = ConfigDict(
        strict=False,
        validate_assignment=True,
        extra="ignore",
        revalidate_instances="never",
    )

    enabled: bool = True
    pip_install: bool = True
    actor_attribution: Literal["per_call", "per_actor"] = "per_call"


RAY_CONFIGURABLE_KEYS = {
    "ray.enabled": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Enable automatic Ray runtime instrumentation",
    ),
    "ray.pip_install": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Inject roar-cli into Ray runtime_env.pip for remote workers",
    ),
    "ray.actor_attribution": ConfigurableKeySpec(
        value_type=str,
        default="per_call",
        description="Ray actor attribution mode: per_call (default) or per_actor",
    ),
}

RAY_INIT_TEMPLATE = """\
[ray]
# Enable automatic Ray worker instrumentation
enabled = true
# Inject roar-cli into runtime_env.pip for remote workers
pip_install = true
# Actor attribution mode for Ray actor methods (per_call | per_actor)
actor_attribution = "per_call"
"""


def normalize_ray_backend_config(section: Mapping[str, Any] | None) -> dict[str, Any]:
    return RayBackendConfig.model_validate(dict(section or {})).model_dump()


def load_ray_backend_config(start_dir: str | None = None) -> dict[str, Any]:
    try:
        from roar.config import load_config

        config = load_config(start_dir=start_dir)
    except Exception:
        return dict(RAY_BACKEND_CONFIG.default_values)

    section = config.get("ray", {})
    if not isinstance(section, Mapping):
        return dict(RAY_BACKEND_CONFIG.default_values)
    return normalize_ray_backend_config(section)


RAY_BACKEND_CONFIG = BackendConfigAdapter(
    section_name="ray",
    default_values=RayBackendConfig().model_dump(),
    configurable_keys=RAY_CONFIGURABLE_KEYS,
    init_template=RAY_INIT_TEMPLATE,
    normalize_section=normalize_ray_backend_config,
)


__all__ = [
    "RAY_BACKEND_CONFIG",
    "RAY_CONFIGURABLE_KEYS",
    "RAY_INIT_TEMPLATE",
    "RayBackendConfig",
    "load_ray_backend_config",
    "normalize_ray_backend_config",
]
