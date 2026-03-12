from __future__ import annotations

from roar.backends.ray.plugin import RAY_EXECUTION_BACKEND
from roar.cli.commands.init import build_default_config_template
from roar.execution.framework import iter_execution_backends, maybe_rewrite_submit_command
from roar.execution.framework.registry import (
    iter_execution_backend_config_adapters,
    iter_execution_backend_configurable_keys,
)


def test_canonical_execution_framework_imports_are_available() -> None:
    assert callable(maybe_rewrite_submit_command)
    assert RAY_EXECUTION_BACKEND.name == "ray"
    assert any(backend.name == "ray" for backend in iter_execution_backends())


def test_backend_config_registration_is_available_through_framework() -> None:
    adapters = iter_execution_backend_config_adapters()
    assert any(adapter.section_name == "ray" for adapter in adapters)
    assert "ray.pip_install" in iter_execution_backend_configurable_keys()


def test_init_template_includes_backend_registered_sections() -> None:
    template = build_default_config_template()

    assert "[ray]" in template
    assert 'actor_attribution = "per_call"' in template
