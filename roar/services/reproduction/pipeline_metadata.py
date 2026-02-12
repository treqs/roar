"""
Pipeline metadata parsing utilities for reproduction workflows.

Provides a single parsing path for package/runtime metadata so callers
do not duplicate JSON parsing and package extraction logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RequirementSummary:
    """Packages required to recreate an environment."""

    build_dpkg: dict[str, str]
    dpkg: dict[str, str]
    build_pip: dict[str, str]
    pip: list[str]


class PipelineMetadataParser:
    """Parse step metadata from local/remote pipeline representations."""

    def extract_manager_packages(self, steps: list[dict], manager: str) -> dict[str, str]:
        """Extract {name: version} packages for one manager from ordered steps."""
        result: dict[str, str] = {}
        for step in steps:
            metadata = self._normalize_metadata(step.get("metadata"))
            packages_by_manager = metadata.get("packages", {})
            manager_packages = packages_by_manager.get(manager, {})
            if isinstance(manager_packages, dict):
                for name, version in manager_packages.items():
                    if name and name not in result:
                        result[name] = version or ""
        return result

    def collect_manager_packages(
        self, build_steps: list[dict], run_steps: list[dict], manager: str
    ) -> dict[str, str]:
        """Collect packages from build+run steps, with run values overriding build."""
        build_pkgs = self.extract_manager_packages(build_steps, manager)
        run_pkgs = self.extract_manager_packages(run_steps, manager)
        return dict(sorted({**build_pkgs, **run_pkgs}.items()))

    def collect_pip_specifiers(self, build_steps: list[dict], run_steps: list[dict]) -> list[str]:
        """Collect pip package specifiers ('name' or 'name==version') from all steps."""
        specs: set[str] = set()
        for step in [*build_steps, *run_steps]:
            metadata = self._normalize_metadata(step.get("metadata"))
            packages_by_manager = metadata.get("packages", {})
            pip_packages = packages_by_manager.get("pip", {})
            if isinstance(pip_packages, dict):
                for name, version in pip_packages.items():
                    if name:
                        specs.add(f"{name}=={version}" if version else name)
        return sorted(specs)

    def first_runtime(self, build_steps: list[dict], run_steps: list[dict]) -> dict[str, Any]:
        """Return first runtime block found in ordered build+run steps."""
        for step in [*build_steps, *run_steps]:
            metadata = self._normalize_metadata(step.get("metadata"))
            runtime = metadata.get("runtime", {})
            if isinstance(runtime, dict) and runtime:
                return runtime
        return {}

    def summarize_requirements(
        self, build_steps: list[dict], run_steps: list[dict]
    ) -> RequirementSummary:
        """Build a package summary used by reproduction setup and preview."""
        return RequirementSummary(
            build_dpkg=self.collect_manager_packages(build_steps, run_steps, "build_dpkg"),
            dpkg=self.collect_manager_packages(build_steps, run_steps, "dpkg"),
            build_pip=self.collect_manager_packages(build_steps, run_steps, "build_pip"),
            pip=self.collect_pip_specifiers(build_steps, run_steps),
        )

    def _normalize_metadata(self, metadata: Any) -> dict[str, Any]:
        if isinstance(metadata, dict):
            return metadata
        if isinstance(metadata, str):
            try:
                parsed = json.loads(metadata)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}
