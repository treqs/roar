"""Compatibility shim for the canonical Ray backend plugin."""

from roar.backends.ray.plugin import (
    RAY_EXECUTION_BACKEND,
    ray_build_driver_proxy_fragment,
    ray_prepare_worker_runtime_env,
    ray_reconstituter_factory,
    ray_should_start_driver_proxy,
    register,
)

__all__ = [
    "RAY_EXECUTION_BACKEND",
    "ray_build_driver_proxy_fragment",
    "ray_prepare_worker_runtime_env",
    "ray_reconstituter_factory",
    "ray_should_start_driver_proxy",
    "register",
]
