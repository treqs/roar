"""
Pydantic Settings for roar configuration.

Provides settings loading from TOML files, environment variables, and defaults.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from ...core.logging import get_logger
from .schema import (
    AnalyzersConfig,
    CleanupConfig,
    CompositesConfig,
    FiltersConfig,
    GlaasConfig,
    HashConfig,
    LoggingConfig,
    OutputConfig,
    ProxyConfig,
    RegisterConfig,
    ReversibleConfig,
    TelemetryConfig,
    TracerConfig,
)


def _infer_search_stop(start: Path) -> Path:
    """Infer a safe upward-search boundary for config/.roar discovery."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            cwd=start,
            text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        # Outside a git repo or git unavailable: keep search local to current start.
        pass
    return start.resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Merge *override* into *base* in-place. Nested dicts are merged recursively;
    all other values (including lists) are replaced by the override."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def find_config_file(start_dir: str | None = None, stop_dir: str | None = None) -> Path | None:
    """
    Find .roar/config.toml by walking up from start_dir (or cwd).

    Args:
        start_dir: Directory to start searching from.
        stop_dir: Optional boundary directory to stop searching at (inclusive).

    Returns:
        Path to config file, or None if not found.
    """
    start = Path(start_dir) if start_dir else Path.cwd()
    stop_path = Path(stop_dir).resolve() if stop_dir else _infer_search_stop(start)

    for parent in [start, *list(start.parents)]:
        # Check for .roar/config.toml
        config_path = parent / ".roar" / "config.toml"
        if config_path.exists():
            return config_path

        # Also check for pyproject.toml with [tool.roar] section
        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                if "tool" in data and "roar" in data["tool"]:
                    return pyproject
            except tomllib.TOMLDecodeError as e:
                get_logger().debug("Failed to parse pyproject.toml at %s: %s", pyproject, e)
            except OSError as e:
                get_logger().debug("Failed to read pyproject.toml at %s: %s", pyproject, e)
        if parent.resolve() == stop_path:
            break

    return None


def find_roar_dir(start_dir: str | None = None, stop_dir: str | None = None) -> Path | None:
    """
    Find an existing .roar directory by walking up from start_dir (or cwd).

    Args:
        start_dir: Directory to start searching from.
        stop_dir: Optional boundary directory to stop searching at (inclusive).

    Returns:
        Path to the nearest .roar directory within the search scope, or None if not found.
    """
    start = Path(start_dir) if start_dir else Path.cwd()

    stop_path = Path(stop_dir).resolve() if stop_dir else _infer_search_stop(start)

    for parent in [start, *list(start.parents)]:
        roar_dir = parent / ".roar"
        if roar_dir.is_dir():
            return roar_dir
        if parent.resolve() == stop_path:
            break

    return None


class TomlConfigSource(PydanticBaseSettingsSource):
    """Custom settings source that loads from TOML config files."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        config_path: Path | None = None,
        start_dir: str | None = None,
    ):
        super().__init__(settings_cls)
        self._config_path = config_path
        self._start_dir = start_dir
        self._data: dict[str, Any] | None = None

    def _load_toml(self) -> dict[str, Any]:
        """Load and cache TOML data.

        Merges two layers:
        1. ``.roarconfig`` at repo root (project-level, committed)
        2. ``.roar/config.toml`` (local, gitignored) — overrides .roarconfig

        List-valued keys in .roarconfig (like ``filters.ignore_paths``) are
        used as the base; .roar/config.toml can override them entirely.
        """
        if self._data is not None:
            return self._data

        self._data = {}

        # Find the primary config file (.roar/config.toml or pyproject.toml)
        path = self._config_path
        if path is None:
            path = find_config_file(self._start_dir)

        # Load .roarconfig as the base layer (project-level, committed).
        roarconfig_data = self._load_roarconfig(path)
        if roarconfig_data:
            self._data = roarconfig_data

        if path is None:
            return self._data

        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)

            # Handle pyproject.toml vs .roar/config.toml
            if path.name == "pyproject.toml":
                data = data.get("tool", {}).get("roar", {})

            # Merge: local config overrides .roarconfig.
            _deep_merge(self._data, data)
            self._data["_config_file"] = str(path)

        except tomllib.TOMLDecodeError as e:
            get_logger().warning("Failed to parse config file %s: %s", path, e)
            self._data["_config_error"] = f"Failed to parse config file: {e}"
        except OSError as e:
            get_logger().warning("Failed to read config file %s: %s", path, e)
            self._data["_config_error"] = f"Failed to read config file: {e}"

        return self._data

    @staticmethod
    def _load_roarconfig(local_config_path: Path | None) -> dict[str, Any]:
        """Load .roarconfig from the repo root if it exists."""
        # Infer repo root from the local config path or cwd.
        if local_config_path and local_config_path.name == "config.toml":
            # .roar/config.toml → repo root is .roar/..
            repo_root = local_config_path.parent.parent
        elif local_config_path:
            repo_root = local_config_path.parent
        else:
            repo_root = Path.cwd()

        roarconfig = repo_root / ".roarconfig"
        if not roarconfig.exists():
            return {}

        try:
            with open(roarconfig, "rb") as f:
                return tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError) as e:
            get_logger().debug("Failed to load .roarconfig at %s: %s", roarconfig, e)
            return {}

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Get field value from TOML data."""
        data = self._load_toml()
        field_value = data.get(field_name)
        return field_value, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Return all TOML data for settings initialization."""
        return self._load_toml()


class RoarSettings(BaseSettings):
    """Roar configuration settings with TOML and environment variable support.

    Priority (highest to lowest):
    1. Explicit init values
    2. Environment variables (ROAR_<section>__<field>)
    3. TOML config file (.roar/config.toml or pyproject.toml [tool.roar])
    4. Model defaults
    """

    model_config = {
        "env_prefix": "ROAR_",
        "env_nested_delimiter": "__",
        "extra": "ignore",
    }

    # Config sections
    output: OutputConfig = OutputConfig()
    analyzers: AnalyzersConfig = AnalyzersConfig()
    filters: FiltersConfig = FiltersConfig()
    cleanup: CleanupConfig = CleanupConfig()
    glaas: GlaasConfig = GlaasConfig()
    registration: RegisterConfig = RegisterConfig()
    hash: HashConfig = HashConfig()
    proxy: ProxyConfig = ProxyConfig()
    tracer: TracerConfig = TracerConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    reversible: ReversibleConfig = ReversibleConfig()
    logging: LoggingConfig = LoggingConfig()
    composites: CompositesConfig = CompositesConfig()
    env: dict[str, str] = {}
    _backend_configs: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)
    _backend_config_source: dict[str, Any] = PrivateAttr(default_factory=dict)
    _backend_configs_loaded: bool = PrivateAttr(default=False)

    # Internal fields (not from config)
    _config_file: str | None = None
    _config_error: str | None = None

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_env_vars(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Handle legacy environment variable aliases."""
        # GLAAS_URL -> glaas.url
        if "GLAAS_URL" in os.environ:
            glaas = data.get("glaas", {})
            if isinstance(glaas, dict):
                if "url" not in glaas or glaas["url"] is None:
                    glaas["url"] = os.environ["GLAAS_URL"]
                data["glaas"] = glaas
            elif isinstance(glaas, GlaasConfig):
                if glaas.url is None:
                    data["glaas"] = GlaasConfig(url=os.environ["GLAAS_URL"], key=glaas.key)

        # ROAR_SSH_KEY -> glaas.key
        if "ROAR_SSH_KEY" in os.environ:
            glaas = data.get("glaas", {})
            if isinstance(glaas, dict):
                if "key" not in glaas or glaas["key"] is None:
                    glaas["key"] = os.environ["ROAR_SSH_KEY"]
                data["glaas"] = glaas
            elif isinstance(glaas, GlaasConfig):
                if glaas.key is None:
                    data["glaas"] = GlaasConfig(url=glaas.url, key=os.environ["ROAR_SSH_KEY"])

        # Store internal fields
        if "_config_file" in data:
            pass  # Will be handled by __init__
        if "_config_error" in data:
            pass  # Will be handled by __init__

        return data

    def __init__(
        self,
        config_path: Path | None = None,
        start_dir: str | None = None,
        **kwargs: Any,
    ):
        """Initialize settings with optional config path override."""
        # Store for later access
        self._init_config_path = config_path
        self._init_start_dir = start_dir
        super().__init__(**kwargs)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize settings sources to add TOML loading.

        Note: We can't pass config_path/start_dir here easily, so we use
        a module-level variable as a workaround.
        """
        toml_source = TomlConfigSource(
            settings_cls,
            config_path=_current_config_path,
            start_dir=_current_start_dir,
        )
        return (
            init_settings,
            env_settings,
            toml_source,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to dict format compatible with legacy config."""
        self._ensure_backend_configs()
        result: dict[str, Any] = {
            "output": self.output.model_dump(),
            "analyzers": self.analyzers.model_dump(),
            "filters": self.filters.model_dump(),
            "cleanup": self.cleanup.model_dump(),
            "glaas": self.glaas.model_dump(),
            "registration": self.registration.model_dump(),
            "hash": self.hash.model_dump(),
            "proxy": self.proxy.model_dump(),
            "tracer": self.tracer.model_dump(),
            "telemetry": self.telemetry.model_dump(),
            "reversible": self.reversible.model_dump(),
            "logging": self.logging.model_dump(),
            "composites": self.composites.model_dump(),
            "env": dict(self.env),
        }
        result.update({key: dict(value) for key, value in self._backend_configs.items()})
        if self._config_file:
            result["_config_file"] = self._config_file
        if self._config_error:
            result["_config_error"] = self._config_error
        return result

    def _ensure_backend_configs(self) -> None:
        if self._backend_configs_loaded:
            return

        self._backend_configs = _resolve_backend_config_sections(self._backend_config_source)
        self._backend_configs_loaded = True


# Module-level variables for passing to settings_customise_sources
_current_config_path: Path | None = None
_current_start_dir: str | None = None


def load_settings(config_path: Path | None = None, start_dir: str | None = None) -> RoarSettings:
    """Load roar settings from config file and environment.

    Args:
        config_path: Explicit path to config file
        start_dir: Directory to start searching from (if config_path not given)

    Returns:
        RoarSettings instance with all sources merged
    """
    global _current_config_path, _current_start_dir

    # Set module-level vars for settings_customise_sources
    _current_config_path = config_path
    _current_start_dir = start_dir

    try:
        settings = RoarSettings(config_path=config_path, start_dir=start_dir)

        # Copy internal fields from TOML source
        toml_source = TomlConfigSource(RoarSettings, config_path, start_dir)
        toml_data = toml_source()
        if "_config_file" in toml_data:
            settings._config_file = toml_data["_config_file"]
        if "_config_error" in toml_data:
            settings._config_error = toml_data["_config_error"]
        settings._backend_config_source = {
            key: value for key, value in toml_data.items() if not str(key).startswith("_")
        }
        settings._backend_configs_loaded = False

        return settings
    finally:
        # Reset module-level vars
        _current_config_path = None
        _current_start_dir = None


def _resolve_backend_config_sections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from roar.execution.framework.registry import resolve_execution_backend_config_sections

    return resolve_execution_backend_config_sections(data)
