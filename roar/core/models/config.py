"""
Configuration models.

Provides Pydantic models for roar configuration with validation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator

from .base import RoarBaseModel

# Type aliases
HashAlgorithm = Literal["blake3", "sha256", "sha512", "md5"]
LogLevel = Literal["debug", "info", "warning", "error"]


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
    quiet: bool = False


class AnalyzersConfig(ConfigBaseModel):
    """Analyzers configuration section."""

    experiment_tracking: bool = True


class FiltersConfig(ConfigBaseModel):
    """File filtering configuration section."""

    ignore_system_reads: bool = True
    ignore_package_reads: bool = True
    ignore_torch_cache: bool = True
    ignore_tmp_files: bool = True


class CleanupConfig(ConfigBaseModel):
    """Cleanup configuration section."""

    delete_tmp_writes: bool = False


class GlaasConfig(ConfigBaseModel):
    """GLaaS server configuration section."""

    url: Annotated[str, Field(max_length=2048)] | None = "https://api.glaas.ai"
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


class RegisterConfig(ConfigBaseModel):
    """Register configuration section for secret filtering during registration."""

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
    hash: HashConfig = Field(default_factory=HashConfig)
    reversible: ReversibleConfig = Field(default_factory=ReversibleConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

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
            if hasattr(obj, part):
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
            if not hasattr(obj, part):
                raise ValueError(f"Unknown config path: {key}")
            obj = getattr(obj, part)

        # Set the final field
        field = parts[-1]
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
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary.

        Returns:
            Configuration as nested dict
        """
        return self.model_dump()
