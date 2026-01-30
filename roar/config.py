"""Configuration loading and management for roar."""

from pathlib import Path

from .core.settings import find_config_file, load_settings

# Valid hash algorithms
VALID_HASH_ALGORITHMS = {"blake3", "sha256", "sha512", "md5"}

# Config keys that can be set via `roar config`
CONFIGURABLE_KEYS = {
    "output.track_repo_files": {
        "type": bool,
        "default": False,
        "description": "Include list of repo files read in provenance output",
    },
    "analyzers.experiment_tracking": {
        "type": bool,
        "default": True,
        "description": "Detect experiment trackers (W&B, MLflow, Neptune)",
    },
    "filters.ignore_system_reads": {
        "type": bool,
        "default": True,
        "description": "Ignore system file reads (/sys, /etc, /sbin)",
    },
    "filters.ignore_package_reads": {
        "type": bool,
        "default": True,
        "description": "Ignore reads from installed packages (already in dependency list)",
    },
    "filters.ignore_torch_cache": {
        "type": bool,
        "default": True,
        "description": "Ignore torch/triton cache reads (/tmp/torchinductor_*, etc.)",
    },
    "filters.ignore_tmp_files": {
        "type": bool,
        "default": True,
        "description": "Ignore /tmp files entirely (overridden by strict mode)",
    },
    "output.quiet": {
        "type": bool,
        "default": False,
        "description": "Suppress written files report after run",
    },
    "cleanup.delete_tmp_writes": {
        "type": bool,
        "default": False,
        "description": "Delete /tmp files written during run (strict mode)",
    },
    "glaas.url": {
        "type": str,
        "default": "https://api.glaas.ai",
        "description": "GLaaS server URL (e.g., https://glaas.example.com)",
    },
    "glaas.web_url": {
        "type": str,
        "default": "https://glaas.ai",
        "description": "GLaaS web UI URL for viewing sessions and artifacts",
    },
    "glaas.key": {
        "type": str,
        "default": None,
        "description": "Path to SSH private key for GLaaS authentication",
    },
    "registration.omit.enabled": {
        "type": bool,
        "default": True,
        "description": "Enable secret filtering for registration data",
    },
    "registration.omit.secrets.values": {
        "type": list,
        "default": [],
        "description": "Explicit secret values to always redact (comma-separated)",
    },
    "registration.omit.env_vars.names": {
        "type": list,
        "default": [
            "WANDB_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
        ],
        "description": "Env var names whose values should be redacted (comma-separated)",
    },
    "registration.omit.allowlist.patterns": {
        "type": list,
        "default": [],
        "description": "Regex patterns that should NOT be redacted (comma-separated)",
    },
    "registration.tagging.enabled": {
        "type": bool,
        "default": True,
        "description": "Create git tag on successful registration",
    },
    "reversible.enabled": {
        "type": bool,
        "default": False,
        "description": "Enable file preservation before overwrites during roar run",
    },
    "hash.primary": {
        "type": str,
        "default": "blake3",
        "description": "Primary hash algorithm (blake3, sha256, sha512, md5)",
    },
    "hash.get": {
        "type": list,
        "default": ["sha256"],
        "description": "Additional algorithms for roar get (comma-separated)",
    },
    "hash.put": {
        "type": list,
        "default": [],
        "description": "Additional algorithms for roar put/upload (comma-separated)",
    },
    "hash.run": {
        "type": list,
        "default": [],
        "description": "Additional algorithms for roar run (comma-separated)",
    },
    "logging.level": {
        "type": str,
        "default": "warning",
        "description": "Log level (debug, info, warning, error)",
    },
    "logging.console": {
        "type": bool,
        "default": False,
        "description": "Output debug logs to stderr",
    },
    "logging.file": {
        "type": bool,
        "default": True,
        "description": "Output debug logs to ~/.roar/roar.log",
    },
}


def _get_default_config() -> dict:
    """Get default config from Pydantic models."""
    from .core.models.config import RoarConfig

    return RoarConfig().to_dict()


def _get_nested(d: dict, key: str, default=None):
    """Get a nested key like 'output.track_repo_files'."""
    parts = key.split(".")
    for part in parts:
        if isinstance(d, dict) and part in d:
            d = d[part]
        else:
            return default
    return d


def _set_nested(d: dict, key: str, value):
    """Set a nested key like 'output.track_repo_files'."""
    parts = key.split(".")
    for part in parts[:-1]:
        if part not in d:
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value


def load_config(config_path: Path | None = None, start_dir: str | None = None) -> dict:
    """
    Load configuration from file.

    Args:
        config_path: Explicit path to config file
        start_dir: Directory to start searching from (if config_path not given)

    Returns:
        Configuration dict with defaults applied
    """
    settings = load_settings(config_path=config_path, start_dir=start_dir)
    return settings.to_dict()


def get_roar_dir(start_dir: str | None = None) -> Path:
    """
    Get the .roar directory path, creating it if needed.

    Returns:
        Path to .roar directory in start_dir or cwd.
    """
    base = Path(start_dir) if start_dir else Path.cwd()
    roar_dir = base / ".roar"
    roar_dir.mkdir(exist_ok=True)
    return roar_dir


def get_config_path_for_write(start_dir: str | None = None) -> Path:
    """
    Get the path where config should be written.

    Prefers existing .roar/config.toml, otherwise creates one in start_dir or cwd.
    """
    existing = find_config_file(start_dir)
    if existing and existing.name == "config.toml":
        return existing

    # Create new .roar/config.toml in start_dir or cwd
    roar_dir = get_roar_dir(start_dir)
    return roar_dir / "config.toml"


def save_config(config: dict, config_path: Path):
    """
    Save configuration to a .roar.toml file.

    Only saves non-default values.
    """
    # Build TOML content manually (to avoid adding tomlkit dependency)
    lines = []

    defaults = _get_default_config()

    # Output section
    output_lines = []
    for key, val in config.get("output", {}).items():
        default_val = defaults.get("output", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                output_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                output_lines.append(f'{key} = "{val}"')
            else:
                output_lines.append(f"{key} = {val}")

    if output_lines:
        lines.append("[output]")
        lines.extend(output_lines)
        lines.append("")

    # Analyzers section
    analyzers_lines = []
    for key, val in config.get("analyzers", {}).items():
        default_val = defaults.get("analyzers", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                analyzers_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                analyzers_lines.append(f'{key} = "{val}"')
            else:
                analyzers_lines.append(f"{key} = {val}")

    if analyzers_lines:
        lines.append("[analyzers]")
        lines.extend(analyzers_lines)
        lines.append("")

    # Filters section
    filters_lines = []
    for key, val in config.get("filters", {}).items():
        default_val = defaults.get("filters", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                filters_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                filters_lines.append(f'{key} = "{val}"')
            else:
                filters_lines.append(f"{key} = {val}")

    if filters_lines:
        lines.append("[filters]")
        lines.extend(filters_lines)
        lines.append("")

    # Cleanup section
    cleanup_lines = []
    for key, val in config.get("cleanup", {}).items():
        default_val = defaults.get("cleanup", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                cleanup_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                cleanup_lines.append(f'{key} = "{val}"')
            else:
                cleanup_lines.append(f"{key} = {val}")

    if cleanup_lines:
        lines.append("[cleanup]")
        lines.extend(cleanup_lines)
        lines.append("")

    # GLaaS section
    glaas_lines = []
    for key, val in config.get("glaas", {}).items():
        default_val = defaults.get("glaas", {}).get(key)
        if val != default_val and val is not None:
            if isinstance(val, bool):
                glaas_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                glaas_lines.append(f'{key} = "{val}"')
            else:
                glaas_lines.append(f"{key} = {val}")

    if glaas_lines:
        lines.append("[glaas]")
        lines.extend(glaas_lines)
        lines.append("")

    # Registration section (for secret filtering config)
    register_config = config.get("registration", {})
    register_defaults = defaults.get("registration", {})

    # Registration.omit section
    omit_config = register_config.get("omit", {})
    omit_defaults = register_defaults.get("omit", {})

    # [registration.omit] enabled
    if omit_config.get("enabled") != omit_defaults.get("enabled"):
        lines.append("[registration.omit]")
        lines.append(f"enabled = {str(omit_config.get('enabled')).lower()}")
        lines.append("")

    # [registration.omit.secrets]
    secrets_vals = omit_config.get("secrets", {}).get("values", [])
    secrets_defaults = omit_defaults.get("secrets", {}).get("values", [])
    if secrets_vals != secrets_defaults and secrets_vals:
        lines.append("[registration.omit.secrets]")
        items = ", ".join(f'"{v}"' for v in secrets_vals)
        lines.append(f"values = [{items}]")
        lines.append("")

    # [registration.omit.env_vars]
    env_vars_names = omit_config.get("env_vars", {}).get("names", [])
    env_vars_defaults = omit_defaults.get("env_vars", {}).get("names", [])
    if env_vars_names != env_vars_defaults and env_vars_names:
        lines.append("[registration.omit.env_vars]")
        items = ", ".join(f'"{v}"' for v in env_vars_names)
        lines.append(f"names = [{items}]")
        lines.append("")

    # [registration.omit.allowlist]
    allowlist_patterns = omit_config.get("allowlist", {}).get("patterns", [])
    allowlist_defaults = omit_defaults.get("allowlist", {}).get("patterns", [])
    if allowlist_patterns != allowlist_defaults and allowlist_patterns:
        lines.append("[registration.omit.allowlist]")
        items = ", ".join(f'"{v}"' for v in allowlist_patterns)
        lines.append(f"patterns = [{items}]")
        lines.append("")

    # [[registration.omit.patterns]] - custom patterns as array of tables
    custom_patterns = omit_config.get("patterns", [])
    patterns_defaults = omit_defaults.get("patterns", [])
    if custom_patterns != patterns_defaults and custom_patterns:
        for p in custom_patterns:
            if isinstance(p, dict):
                lines.append("[[registration.omit.patterns]]")
                if "id" in p:
                    lines.append(f'id = "{p["id"]}"')
                if "pattern" in p:
                    # Escape backslashes for TOML
                    pattern_escaped = p["pattern"].replace("\\", "\\\\")
                    lines.append(f'pattern = "{pattern_escaped}"')
                if "description" in p:
                    lines.append(f'description = "{p["description"]}"')
                lines.append("")

    # [registration.tagging] section
    tagging_config = register_config.get("tagging", {})
    tagging_defaults = register_defaults.get("tagging", {})
    if tagging_config.get("enabled") != tagging_defaults.get("enabled"):
        lines.append("[registration.tagging]")
        lines.append(f"enabled = {str(tagging_config.get('enabled')).lower()}")
        lines.append("")

    # Hash section
    hash_lines = []
    for key, val in config.get("hash", {}).items():
        default_val = defaults.get("hash", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                hash_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                hash_lines.append(f'{key} = "{val}"')
            elif isinstance(val, list):
                # Format as TOML array
                items = ", ".join(f'"{v}"' for v in val)
                hash_lines.append(f"{key} = [{items}]")
            else:
                hash_lines.append(f"{key} = {val}")

    if hash_lines:
        lines.append("[hash]")
        lines.extend(hash_lines)
        lines.append("")

    # Reversible section
    reversible_lines = []
    for key, val in config.get("reversible", {}).items():
        default_val = defaults.get("reversible", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                reversible_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                reversible_lines.append(f'{key} = "{val}"')
            else:
                reversible_lines.append(f"{key} = {val}")

    if reversible_lines:
        lines.append("[reversible]")
        lines.extend(reversible_lines)
        lines.append("")

    # Logging section
    logging_lines = []
    for key, val in config.get("logging", {}).items():
        default_val = defaults.get("logging", {}).get(key)
        if val != default_val:
            if isinstance(val, bool):
                logging_lines.append(f"{key} = {str(val).lower()}")
            elif isinstance(val, str):
                logging_lines.append(f'{key} = "{val}"')
            else:
                logging_lines.append(f"{key} = {val}")

    if logging_lines:
        lines.append("[logging]")
        lines.extend(logging_lines)
        lines.append("")

    # Env section (persistent environment variables)
    env_vars = config.get("env", {})
    if isinstance(env_vars, dict) and env_vars:
        env_lines = []
        for key, val in env_vars.items():
            env_lines.append(f'{key} = "{val}"')
        if env_lines:
            lines.append("[env]")
            lines.extend(env_lines)
            lines.append("")

    config_path.write_text("\n".join(lines))


def config_get(key: str, start_dir: str | None = None):
    """Get a config value."""
    config = load_config(start_dir=start_dir)
    return _get_nested(config, key)


def config_set(key: str, value: str, start_dir: str | None = None):
    """Set a config value and save to .roar.toml."""
    from typing import Any

    if key not in CONFIGURABLE_KEYS:
        raise ValueError(
            f"Unknown config key: {key}. Valid keys: {', '.join(CONFIGURABLE_KEYS.keys())}"
        )

    key_info = CONFIGURABLE_KEYS[key]
    typed_value: Any

    # Parse value to correct type
    if key_info["type"] is bool:  # type: ignore[index]
        if value.lower() in ("true", "1", "yes", "on"):
            typed_value = True
        elif value.lower() in ("false", "0", "no", "off"):
            typed_value = False
        else:
            raise ValueError(f"Invalid boolean value: {value}")
    elif key_info["type"] is list:  # type: ignore[index]
        # Parse comma-separated list
        if value.strip() == "":
            typed_value = []
        else:
            typed_value = [v.strip() for v in value.split(",")]
        # Validate hash algorithms if this is a hash config key
        if key.startswith("hash."):
            for algo in typed_value:
                if algo not in VALID_HASH_ALGORITHMS:
                    raise ValueError(
                        f"Invalid hash algorithm: {algo}. "
                        f"Valid algorithms: {', '.join(sorted(VALID_HASH_ALGORITHMS))}"
                    )
    elif key.startswith("hash.") and key_info["type"] is str:  # type: ignore[index]
        # Validate single hash algorithm
        if value not in VALID_HASH_ALGORITHMS:
            raise ValueError(
                f"Invalid hash algorithm: {value}. "
                f"Valid algorithms: {', '.join(sorted(VALID_HASH_ALGORITHMS))}"
            )
        typed_value = value
    else:
        typed_value = value

    # Load existing config, update, and save
    config = load_config(start_dir=start_dir)
    _set_nested(config, key, typed_value)

    config_path = get_config_path_for_write(start_dir)
    save_config(config, config_path)

    return config_path, typed_value


def config_list():
    """List all configurable keys with descriptions."""
    return CONFIGURABLE_KEYS


def get_hash_algorithms(
    operation: str,
    cli_algorithms: list | None = None,
    hash_only: bool = False,
    start_dir: str | None = None,
) -> list:
    """
    Get the list of hash algorithms to use for an operation.

    Args:
        operation: One of 'get', 'put', 'run'
        cli_algorithms: Algorithms specified via --hash or --hash-only CLI option
        hash_only: If True, use only cli_algorithms (skip primary and config)
        start_dir: Directory to load config from

    Returns:
        List of algorithm names to compute, deduplicated, primary first (unless hash_only)
    """
    if hash_only and cli_algorithms:
        # Validate and return only CLI-specified algorithms
        for algo in cli_algorithms:
            if algo not in VALID_HASH_ALGORITHMS:
                raise ValueError(
                    f"Invalid hash algorithm: {algo}. "
                    f"Valid algorithms: {', '.join(sorted(VALID_HASH_ALGORITHMS))}"
                )
        return cli_algorithms

    config = load_config(start_dir=start_dir)

    # Start with primary algorithm
    primary = config.get("hash", {}).get("primary", "blake3")
    algorithms = [primary]

    # Add operation-specific algorithms from config
    config_algos = config.get("hash", {}).get(operation, [])
    for algo in config_algos:
        if algo not in algorithms:
            algorithms.append(algo)

    # Add CLI-specified algorithms
    if cli_algorithms:
        for algo in cli_algorithms:
            if algo not in VALID_HASH_ALGORITHMS:
                raise ValueError(
                    f"Invalid hash algorithm: {algo}. "
                    f"Valid algorithms: {', '.join(sorted(VALID_HASH_ALGORITHMS))}"
                )
            if algo not in algorithms:
                algorithms.append(algo)

    return algorithms
