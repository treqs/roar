from __future__ import annotations

import sys
from typing import Any, cast


def patch_tracked_ray_module(module_name: str, module: Any) -> None:
    del module_name

    ray_module = sys.modules.get("ray")
    if ray_module is None and hasattr(module, "init"):
        ray_module = module
    if ray_module is None or getattr(ray_module, "_roar_runtime_patched", False):
        return
    if not hasattr(ray_module, "init"):
        return

    from roar.services.execution.inject import sitecustomize

    sitecustomize._patch_ray_init(ray_module)
    sitecustomize._patch_ray_shutdown(ray_module)
    cast(Any, ray_module)._roar_runtime_patched = True
