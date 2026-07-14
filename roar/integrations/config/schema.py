"""
Configuration models.

Provides Pydantic models for roar configuration with validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, PrivateAttr, field_validator

from ...core.models.base import RoarBaseModel
from ...core.tracer_modes import TracerMode

# Type aliases
HashAlgorithm = Literal["blake3", "sha256", "sha512", "md5"]
LogLevel = Literal["debug", "info", "warning", "error"]
Verbosity = Literal["quiet", "normal", "verbose", "debug"]


class ConfigBaseModel(RoarBaseModel):
    """Base model for config sections with relaxed strict mode for TOML loading."""

    model_config = ConfigDict(
        strict=False,  # Allow coercion from TOML types
        validate_assignment=True,
        extra="ignore",  # Ignore unknown fields in config files
        populate_by_name=True,
        use_enum_values=True,
        revalidate_instances="never",
    )


class OutputConfig(ConfigBaseModel):
    """Output configuration section."""

    track_repo_files: bool = False
    # Deprecated. `verbosity` is the canonical knob. `quiet=true` maps to
    # `verbosity="quiet"`. Kept for backward compatibility; will be removed
    # in a future release.
    quiet: bool = False
    verbosity: Verbosity = "normal"


class AnalyzersConfig(ConfigBaseModel):
    """Analyzers configuration section."""

    experiment_tracking: bool = True


class FiltersConfig(ConfigBaseModel):
    """File filtering configuration section."""

    ignore_system_reads: bool = True
    ignore_package_reads: bool = True
    ignore_torch_cache: bool = True
    ignore_library_caches: bool = True
    ignore_tmp_files: bool = True
    ignore_paths: list[str] = Field(default_factory=list)


class CleanupConfig(ConfigBaseModel):
    """Cleanup configuration section."""

    delete_tmp_writes: bool = False


class GlaasConfig(ConfigBaseModel):
    """GLaaS server configuration section."""

    url: Annotated[str, Field(max_length=2048)] | None = "https://api.glaas.ai"
    query_nonlocal_ids_on_glaas: bool = False
    key: str | None = None  # SSH private key path

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        """Validate and normalize GLaaS URL."""
        if v is None or v == "":
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("GLaaS URL must start with http:// or https://")
        return v.rstrip("/")


class ReversibleConfig(ConfigBaseModel):
    """Reversible file preservation configuration section."""

    enabled: bool = False


# Sync omit nested structures
class SecretsConfig(ConfigBaseModel):
    """Secrets to always redact."""

    values: list[str] = Field(default_factory=list)


class EnvVarsConfig(ConfigBaseModel):
    """Environment variables whose values should be redacted."""

    names: list[str] = Field(
        default_factory=lambda: [
            "WANDB_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
        ]
    )


class AllowlistConfig(ConfigBaseModel):
    """Patterns that should NOT be redacted."""

    patterns: list[str] = Field(default_factory=list)


class CustomPattern(ConfigBaseModel):
    """Custom regex pattern for secret detection."""

    id: str
    pattern: str
    description: str | None = None


class OmitConfig(ConfigBaseModel):
    """Secret omission/filtering configuration."""

    enabled: bool = True
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    env_vars: EnvVarsConfig = Field(default_factory=EnvVarsConfig)
    patterns: list[CustomPattern] = Field(default_factory=list)
    allowlist: AllowlistConfig = Field(default_factory=AllowlistConfig)


class TaggingConfig(ConfigBaseModel):
    """Git tagging configuration for registration."""

    enabled: bool = True


class TagsConfig(ConfigBaseModel):
    """Hereditary compliance-tag (``roar tag``) configuration.

    ``custom_kinds`` extends the built-in canonical kinds with project-specific
    hereditary kinds. Committed in ``.roarconfig`` so a team shares the same
    allowed set. Distinct from ``registration.tagging`` (git tags at register).
    """

    custom_kinds: list[str] = Field(default_factory=list)


class RegisterConfig(ConfigBaseModel):
    """Register/publish defaults and filtering configuration."""

    public_by_default: bool = False
    omit: OmitConfig = Field(default_factory=OmitConfig)
    tagging: TaggingConfig = Field(default_factory=TaggingConfig)


class HashConfig(ConfigBaseModel):
    """Hash algorithm configuration section."""

    primary: HashAlgorithm = "blake3"
    get: list[HashAlgorithm] = Field(default_factory=lambda: ["sha256"])  # type: ignore[arg-type]
    put: list[HashAlgorithm] = Field(default_factory=list)
    run: list[HashAlgorithm] = Field(default_factory=list)

    @field_validator("get", "put", "run", mode="before")
    @classmethod
    def parse_comma_separated(cls, v: Any) -> list[str]:
        """Parse comma-separated string to list."""
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v if v else []


class ProxyConfig(ConfigBaseModel):
    """S3 proxy configuration section."""

    enabled: bool = False


class TracerConfig(ConfigBaseModel):
    """Tracer backend configuration section."""

    default: TracerMode = "auto"
    fallback_enabled: bool = True


class TelemetryConfig(ConfigBaseModel):
    """Anonymous product telemetry configuration section."""

    enabled: bool = True
    endpoint: Annotated[str, Field(max_length=2048)] | None = None

    @field_validator("endpoint", mode="before")
    @classmethod
    def validate_endpoint(cls, v: str | None) -> str | None:
        """Validate and normalize an optional telemetry endpoint override."""
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        stripped = v.strip()
        if stripped == "":
            return ""
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("Telemetry endpoint must start with http:// or https://")
        return stripped.rstrip("/")


class GitConfig(ConfigBaseModel):
    """Git integration configuration section."""

    # Canonical remote roar pushes tags to during `roar register`. When
    # unset, roar picks the remote automatically if there's exactly one;
    # multiple remotes require an explicit value.
    remote: str | None = None
    # Whether to push roar-namespaced tags during `roar register`.
    # "auto" (default): push to git.remote, fail-closed if push fails.
    # "never": skip push entirely — GLaaS records will reference tags
    # that exist only on the registering machine.
    push_tags_on_register: Literal["auto", "never"] = "auto"


class HintsConfig(ConfigBaseModel):
    """Global toggle for hint blocks roar prints during routine commands.

    Inspired by git's `advice.*` family but coarse-grained — one knob
    silences every advisory output (init hints, etc.). Disable when you
    know what you're doing; the underlying commands still work.

    Hints are also auto-suppressed in quiet output mode and when stdout
    isn't a TTY (so CI logs stay terse).
    """

    enabled: bool = True


class LoggingConfig(ConfigBaseModel):
    """Logging configuration section."""

    level: LogLevel = "warning"
    console: bool = False
    file: bool = True


class RoarConfig(ConfigBaseModel):
    """Complete roar configuration.

    This model represents the full configuration with all sections.
    It can be loaded from TOML files or constructed programmatically.
    """

    output: OutputConfig = Field(default_factory=OutputConfig)
    analyzers: AnalyzersConfig = Field(default_factory=AnalyzersConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    glaas: GlaasConfig = Field(default_factory=GlaasConfig)
    registration: RegisterConfig = Field(default_factory=RegisterConfig)
    tags: TagsConfig = Field(default_factory=TagsConfig)
    hash: HashConfig = Field(default_factory=HashConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    tracer: TracerConfig = Field(default_factory=TracerConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    hints: HintsConfig = Field(default_factory=HintsConfig)
    reversible: ReversibleConfig = Field(default_factory=ReversibleConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    _backend_configs: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._backend_configs = _resolve_backend_config_sections({})

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by dot-notation key.

        Args:
            key: Dot-notation key (e.g., 'output.quiet')
            default: Default value if key not found

        Returns:
            Config value or default
        """
        parts = key.split(".")
        obj: Any = self
        for part in parts:
            if isinstance(obj, RoarConfig) and part in self._backend_configs:
                obj = self._backend_configs[part]
            elif hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                return default
        return obj

    def set(self, key: str, value: Any) -> None:
        """Set config value by dot-notation key.

        Args:
            key: Dot-notation key (e.g., 'output.quiet', 'registration.omit.enabled')
            value: Value to set

        Raises:
            ValueError: If key path is invalid
        """
        parts = key.split(".")
        if len(parts) < 2:
            raise ValueError(f"Invalid config key: {key}")

        # Navigate to parent object
        obj: Any = self
        for part in parts[:-1]:
            if isinstance(obj, RoarConfig) and part in self._backend_configs:
                obj = self._backend_configs[part]
                continue
            if hasattr(obj, part):
                obj = getattr(obj, part)
                continue
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
                continue
            raise ValueError(f"Unknown config path: {key}")

        # Set the final field
        field = parts[-1]
        if isinstance(obj, dict):
            obj[field] = value
            return
        if not hasattr(obj, field):
            raise ValueError(f"Unknown config field: {key}")
        setattr(obj, field, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoarConfig:
        """Create config from dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            RoarConfig instance
        """
        core_data, backend_sections = _split_backend_config_sections(data)
        config = cls.model_validate(core_data)
        config._backend_configs = _resolve_backend_config_sections(backend_sections)
        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary.

        Returns:
            Configuration as nested dict
        """
        result = self.model_dump()
        result.update({key: dict(value) for key, value in self._backend_configs.items()})
        return result


def _resolve_backend_config_sections(data: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    from roar.execution.framework.registry import resolve_execution_backend_config_sections

    return resolve_execution_backend_config_sections(data)


def _split_backend_config_sections(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from roar.execution.framework.registry import iter_execution_backend_section_names

    backend_sections = set(iter_execution_backend_section_names())
    core_data: dict[str, Any] = {}
    backend_data: dict[str, Any] = {}
    for key, value in data.items():
        if key in backend_sections:
            backend_data[key] = value
            continue
        core_data[key] = value
    return core_data, backend_data
