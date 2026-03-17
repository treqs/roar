from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from roar.backends.local.plugin import LOCAL_EXECUTION_BACKEND
from roar.backends.osmo.plugin import OSMO_EXECUTION_BACKEND
from roar.backends.ray.plugin import RAY_EXECUTION_BACKEND
from roar.cli.commands.init import build_default_config_template
from roar.execution.framework import iter_execution_backends, plan_execution_command
from roar.execution.framework.registry import (
    iter_execution_backend_config_adapters,
    iter_execution_backend_configurable_keys,
)
from roar.execution.recording import DatasetIdentifierInferer, ExecutionJobRecorder


def test_canonical_execution_framework_imports_are_available() -> None:
    assert callable(plan_execution_command)
    assert DatasetIdentifierInferer is not None
    assert ExecutionJobRecorder is not None
    assert LOCAL_EXECUTION_BACKEND.name == "local"
    assert OSMO_EXECUTION_BACKEND.name == "osmo"
    assert RAY_EXECUTION_BACKEND.name == "ray"
    assert any(backend.name == "ray" for backend in iter_execution_backends())
    assert any(backend.name == "osmo" for backend in iter_execution_backends())


def test_backend_config_registration_is_available_through_framework() -> None:
    adapters = iter_execution_backend_config_adapters()
    assert any(adapter.section_name == "ray" for adapter in adapters)
    assert any(adapter.section_name == "osmo" for adapter in adapters)
    assert "ray.pip_install" in iter_execution_backend_configurable_keys()
    assert "osmo.enabled" in iter_execution_backend_configurable_keys()
    assert "osmo.auto_prepare_submissions" in iter_execution_backend_configurable_keys()
    assert "osmo.force_json_output" in iter_execution_backend_configurable_keys()
    assert "osmo.wait_for_completion" in iter_execution_backend_configurable_keys()
    assert "osmo.download_declared_outputs" in iter_execution_backend_configurable_keys()
    assert "osmo.download_directory" in iter_execution_backend_configurable_keys()
    assert "osmo.ingest_lineage_bundles" in iter_execution_backend_configurable_keys()
    assert "osmo.lineage_bundle_dataset_name" in iter_execution_backend_configurable_keys()
    assert "osmo.lineage_bundle_filename" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_requirement" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_local_path" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_remote_path" in iter_execution_backend_configurable_keys()


def test_init_template_includes_backend_registered_sections() -> None:
    template = build_default_config_template()

    assert "[ray]" in template
    assert "[osmo]" in template
    assert 'actor_attribution = "per_call"' in template
    assert "auto_prepare_submissions = true" in template
    assert "force_json_output = true" in template
    assert "wait_for_completion = false" in template
    assert "download_declared_outputs = false" in template
    assert "ingest_lineage_bundles = false" in template
    assert 'lineage_bundle_dataset_name = "roar-lineage"' in template
    assert 'runtime_install_requirement = ""' in template
    assert 'runtime_install_local_path = ""' in template


def test_packaged_roar_worker_entrypoint_uses_canonical_runtime_module() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    )

    scripts = pyproject["project"]["scripts"]

    assert scripts["roar-worker"] == "roar.execution.runtime.worker_bootstrap:main"


def test_shared_test_tooling_uses_backend_agnostic_product_path_globs() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    )

    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    per_file_ignores = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]

    assert "--ignore-glob=tests/backends/*/e2e" in addopts
    assert "--ignore-glob=tests/backends/*/live" in addopts
    assert "tests/backends/*/e2e/jobs/**/*.py" in per_file_ignores
