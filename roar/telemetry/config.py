"""Effective telemetry configuration and suppression rules."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from .capabilities import DEFAULT_TELEMETRY_ENDPOINT
from .paths import TelemetryPaths, resolve_paths

CI_ENV_MARKERS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "TRAVIS",
    "APPVEYOR",
    "TEAMCITY_VERSION",
    "JENKINS_URL",
    "TF_BUILD",
    "BITBUCKET_BUILD_NUMBER",
    "CODEBUILD_BUILD_ID",
    "DRONE",
    "HEROKU_TEST_RUN_ID",
    "PYTEST_CURRENT_TEST",
)

BACKEND_JOB_ENV_MARKERS = (
    "RAY_JOB_ID",
    "OSMO_JOB_ID",
    "OSMO_WORKFLOW_ID",
)


@dataclass(frozen=True)
class EffectiveTelemetryConfig:
    """Resolved telemetry state after env/global/project/default precedence."""

    enabled: bool
    endpoint: str
    endpoint_source: str
    enabled_source: str
    disabled_reason: str | None
    suppression_reason: str | None
    global_config_path: Path
    project_config_path: Path | None

    @property
    def stats_enabled(self) -> bool:
        return self.enabled and self.disabled_reason is None and self.suppression_reason is None

    @property
    def uploads_enabled(self) -> bool:
        return self.stats_enabled and bool(self.endpoint)


def resolve_config(
    *,
    start_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
) -> EffectiveTelemetryConfig:
    """Resolve effective telemetry config.

    Precedence is environment, global config, project config, then defaults.
    Project config can opt down from defaults, but cannot beat global or env state.
    """

    resolved_env = os.environ if environ is None else environ
    resolved_paths = paths or resolve_paths(resolved_env)

    endpoint = DEFAULT_TELEMETRY_ENDPOINT
    endpoint_source = "default"
    enabled = True
    enabled_source = "default"
    disabled_reason: str | None = None

    project_config_path, project_data = _load_project_config(start_dir)
    project_telemetry = _telemetry_section(project_data)
    project_enabled = _optional_bool(project_telemetry.get("enabled"))
    if project_enabled is False:
        enabled = False
        enabled_source = "project"
        disabled_reason = "project_disabled"
    project_endpoint = _optional_endpoint(project_telemetry.get("endpoint"))
    if project_endpoint is not None:
        endpoint = project_endpoint
        endpoint_source = "project"

    global_data = _load_config_file(resolved_paths.global_config_file)
    global_telemetry = _telemetry_section(global_data)
    global_enabled = _optional_bool(global_telemetry.get("enabled"))
    if global_enabled is not None:
        enabled = global_enabled
        enabled_source = "global"
        disabled_reason = None if global_enabled else "global_disabled"
    global_endpoint = _optional_endpoint(global_telemetry.get("endpoint"))
    if global_endpoint is not None:
        endpoint = global_endpoint
        endpoint_source = "global"

    if "ROAR_TELEMETRY_ENDPOINT" in resolved_env:
        endpoint = str(resolved_env.get("ROAR_TELEMETRY_ENDPOINT") or "").strip()
        endpoint_source = "env"

    suppression_reason = suppression_reason_for(resolved_env)
    if suppression_reason is None and _cache_is_inside_project_tree(start_dir, resolved_paths):
        suppression_reason = "project_local_cache"
    if suppression_reason is not None:
        enabled = False
        enabled_source = (
            "env"
            if suppression_reason in {"do_not_track", "roar_no_telemetry"}
            else "suppression"
        )

    return EffectiveTelemetryConfig(
        enabled=enabled,
        endpoint=endpoint,
        endpoint_source=endpoint_source,
        enabled_source=enabled_source,
        disabled_reason=disabled_reason,
        suppression_reason=suppression_reason,
        global_config_path=resolved_paths.global_config_file,
        project_config_path=project_config_path,
    )


def suppression_reason_for(environ: Mapping[str, str] | None = None) -> str | None:
    resolved_env = os.environ if environ is None else environ
    if _truthy_env_value(resolved_env.get("DO_NOT_TRACK")):
        return "do_not_track"
    if _truthy_env_value(resolved_env.get("ROAR_NO_TELEMETRY")):
        return "roar_no_telemetry"
    for marker in CI_ENV_MARKERS:
        if _truthy_env_value(resolved_env.get(marker)):
            return f"ci:{marker}"
    if _truthy_env_value(resolved_env.get("ROAR_JOB_INSTRUMENTED")):
        return "roar_job_instrumented"
    for marker in BACKEND_JOB_ENV_MARKERS:
        if marker in resolved_env:
            return f"backend_job_environment:{marker}"
    if _execution_backend_job_environment(resolved_env):
        return "backend_job_environment:registered"
    return None


def set_global_enabled(
    enabled: bool,
    *,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
) -> Path:
    resolved_paths = paths or resolve_paths(environ)
    _upsert_telemetry_values(resolved_paths.global_config_file, {"enabled": bool(enabled)})
    return resolved_paths.global_config_file


def set_global_endpoint(
    endpoint: str,
    *,
    environ: Mapping[str, str] | None = None,
    paths: TelemetryPaths | None = None,
) -> Path:
    resolved_paths = paths or resolve_paths(environ)
    _upsert_telemetry_values(resolved_paths.global_config_file, {"endpoint": str(endpoint)})
    return resolved_paths.global_config_file


def _execution_backend_job_environment(environ: Mapping[str, str]) -> bool:
    try:
        from roar.execution.framework.registry import is_execution_backend_job_environment
    except Exception:
        return False
    try:
        return bool(is_execution_backend_job_environment(environ))
    except Exception:
        return False


def _cache_is_inside_project_tree(
    start_dir: str | os.PathLike[str] | None,
    paths: TelemetryPaths,
) -> bool:
    if start_dir is None:
        return False

    try:
        start = Path(start_dir).resolve()
        project_root = _find_project_tree_root(start)
        paths.cache_dir.resolve().relative_to(project_root)
        return True
    except (OSError, ValueError):
        return False


def _find_project_tree_root(start: Path) -> Path:
    for parent in [start, *list(start.parents)]:
        if (parent / ".git").exists():
            return parent
    return start


def _load_project_config(
    start_dir: str | os.PathLike[str] | None,
) -> tuple[Path | None, dict[str, Any]]:
    path = _find_project_config_file(start_dir)
    if path is None:
        return None, {}
    return path, _load_config_file(path)


def _find_project_config_file(start_dir: str | os.PathLike[str] | None) -> Path | None:
    start = Path(start_dir) if start_dir is not None else Path.cwd()
    for parent in [start, *list(start.parents)]:
        config_path = parent / ".roar" / "config.toml"
        if config_path.exists():
            return config_path
        pyproject_path = parent / "pyproject.toml"
        if pyproject_path.exists():
            data = _load_config_file(pyproject_path)
            if data:
                return pyproject_path
    return None


def _load_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}

    if path.name == "pyproject.toml":
        tool = data.get("tool")
        if not isinstance(tool, dict):
            return {}
        roar = tool.get("roar")
        return roar if isinstance(roar, dict) else {}
    return data if isinstance(data, dict) else {}


def _telemetry_section(data: Mapping[str, Any]) -> Mapping[str, Any]:
    telemetry = data.get("telemetry")
    return telemetry if isinstance(telemetry, Mapping) else {}


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _optional_endpoint(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip()
    return None


def _truthy_env_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"0", "false", "no", "off"}


def _upsert_telemetry_values(path: Path, values: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keys = set(values)

    section_start: int | None = None
    section_end = len(original)
    for index, line in enumerate(original):
        stripped = line.strip()
        if stripped == "[telemetry]":
            section_start = index
            continue
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = index
            break

    rendered_values = [f"{key} = {_format_toml_value(values[key])}" for key in sorted(keys)]

    if section_start is None:
        updated = [*original]
        if updated and updated[-1].strip():
            updated.append("")
        updated.append("[telemetry]")
        updated.extend(rendered_values)
    else:
        before = original[: section_start + 1]
        section = [
            line
            for line in original[section_start + 1 : section_end]
            if line.split("=", 1)[0].strip() not in keys
        ]
        after = original[section_end:]
        updated = [*before, *section, *rendered_values, *after]

    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))
