from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from roar.execution.framework.contract import BackendConfigAdapter, ConfigurableKeySpec


class OsmoBackendConfig(BaseModel):
    """OSMO backend configuration."""

    model_config = ConfigDict(
        strict=False,
        validate_assignment=True,
        extra="ignore",
        revalidate_instances="never",
    )

    enabled: bool = True
    auto_prepare_submissions: bool = True
    force_json_output: bool = True
    wait_for_completion: bool = False
    download_declared_outputs: bool = False
    download_directory: str = ".roar/osmo/downloads"
    ingest_lineage_bundles: bool = False
    lineage_bundle_dataset_name: str = "roar-lineage"
    lineage_bundle_filename: str = "roar-fragments.json"
    runtime_install_requirement: str = ""
    runtime_install_local_path: str = ""
    runtime_install_remote_path: str = "/tmp/roar-osmo-install.whl"
    query_timeout_seconds: int = Field(default=12 * 60, ge=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)


OSMO_CONFIGURABLE_KEYS = {
    "osmo.enabled": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Enable automatic OSMO workflow submit handling in roar run",
    ),
    "osmo.auto_prepare_submissions": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Rewrite local OSMO workflow submits through a temporary Roar-instrumented workflow",
    ),
    "osmo.force_json_output": ConfigurableKeySpec(
        value_type=bool,
        default=True,
        description="Append --format-type json to OSMO workflow submit commands when missing",
    ),
    "osmo.wait_for_completion": ConfigurableKeySpec(
        value_type=bool,
        default=False,
        description="Poll submitted OSMO workflows to a terminal state before completing roar run",
    ),
    "osmo.download_declared_outputs": ConfigurableKeySpec(
        value_type=bool,
        default=False,
        description="Download declared dataset outputs after successful waited OSMO workflow completion",
    ),
    "osmo.download_directory": ConfigurableKeySpec(
        value_type=str,
        default=".roar/osmo/downloads",
        description="Directory for downloaded OSMO dataset outputs, relative to the repo root when not absolute",
    ),
    "osmo.ingest_lineage_bundles": ConfigurableKeySpec(
        value_type=bool,
        default=False,
        description="Ingest downloaded OSMO output bundles named by osmo.lineage_bundle_filename into the local Roar DB",
    ),
    "osmo.lineage_bundle_dataset_name": ConfigurableKeySpec(
        value_type=str,
        default="roar-lineage",
        description="Dataset name Roar uses by default when downloading returned OSMO lineage bundles",
    ),
    "osmo.lineage_bundle_filename": ConfigurableKeySpec(
        value_type=str,
        default="roar-fragments.json",
        description="Filename Roar treats as a downloaded OSMO lineage bundle when ingest_lineage_bundles is enabled",
    ),
    "osmo.runtime_install_requirement": ConfigurableKeySpec(
        value_type=str,
        default="",
        description="Pinned requirement, wheel URL, or install target used by OSMO runtime wrappers when bootstrapping Roar remotely; packaged roar-cli wheels are expected to include tracer binaries",
    ),
    "osmo.runtime_install_local_path": ConfigurableKeySpec(
        value_type=str,
        default="",
        description="Optional local wheel or artifact path injected into prepared OSMO workflows and installed by the wrapper; roar-cli wheels should be built with packaged binaries",
    ),
    "osmo.runtime_install_remote_path": ConfigurableKeySpec(
        value_type=str,
        default="/tmp/roar-osmo-install.whl",
        description="Remote path inside OSMO tasks used for an injected runtime install artifact",
    ),
    "osmo.query_timeout_seconds": ConfigurableKeySpec(
        value_type=int,
        default=12 * 60,
        description="Maximum time to wait for a submitted OSMO workflow to reach a terminal state",
    ),
    "osmo.poll_interval_seconds": ConfigurableKeySpec(
        value_type=float,
        default=5.0,
        description="Polling interval in seconds when waiting for OSMO workflow completion",
    ),
}

OSMO_INIT_TEMPLATE = """\
[osmo]
# Enable OSMO workflow-submit recognition in roar run
enabled = true
# Rewrite local workflow submits through a temporary Roar-instrumented workflow
auto_prepare_submissions = true
# Append --format-type json when missing so Roar can capture workflow metadata
force_json_output = true
# Optionally wait for submitted workflows to reach a terminal state
wait_for_completion = false
# Optionally download declared dataset outputs after a successful waited run
download_declared_outputs = false
# Optionally ingest downloaded lineage bundles back into the local Roar DB
ingest_lineage_bundles = false
# Standard dataset name Roar expects for returned lineage bundles
lineage_bundle_dataset_name = "roar-lineage"
# Optional pinned requirement or wheel URL installed by the injected OSMO wrapper
# For roar-cli, use a packaged wheel or index source that includes bundled tracer binaries
runtime_install_requirement = ""
# Optional local wheel path injected into the workflow and installed by the wrapper
# For roar-cli, prefer a wheel built through scripts/build_wheel_with_bins.sh
runtime_install_local_path = ""
# Remote install-artifact path used inside OSMO tasks when runtime_install_local_path is set
runtime_install_remote_path = "/tmp/roar-osmo-install.whl"
"""


def normalize_osmo_backend_config(section: Mapping[str, Any] | None) -> dict[str, Any]:
    return OsmoBackendConfig.model_validate(dict(section or {})).model_dump()


def load_osmo_backend_config(start_dir: str | None = None) -> dict[str, Any]:
    try:
        from roar.integrations.config import load_config

        config = load_config(start_dir=start_dir)
    except Exception:
        return dict(OSMO_BACKEND_CONFIG.default_values)

    section = config.get("osmo", {})
    if not isinstance(section, Mapping):
        return dict(OSMO_BACKEND_CONFIG.default_values)
    return normalize_osmo_backend_config(section)


OSMO_BACKEND_CONFIG = BackendConfigAdapter(
    section_name="osmo",
    default_values=OsmoBackendConfig().model_dump(),
    configurable_keys=OSMO_CONFIGURABLE_KEYS,
    init_template=OSMO_INIT_TEMPLATE,
    normalize_section=normalize_osmo_backend_config,
)


__all__ = [
    "OSMO_BACKEND_CONFIG",
    "OSMO_CONFIGURABLE_KEYS",
    "OSMO_INIT_TEMPLATE",
    "OsmoBackendConfig",
    "load_osmo_backend_config",
    "normalize_osmo_backend_config",
]
