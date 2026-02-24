"""Configuration loading and management for roar."""

from pathlib import Path
from typing import Any, cast

from .core.settings import find_config_file, find_roar_dir, load_settings
from .core.tracer_modes import VALID_TRACER_MODES

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
    "proxy.enabled": {
        "type": bool,
        "default": False,
        "description": "Enable S3 proxy for lineage tracking during roar run",
    },
    "tracer.default": {
        "type": str,
        "default": "auto",
        "description": "Default tracer backend (auto, ebpf, preload, ptrace)",
    },
    "tracer.fallback_enabled": {
        "type": bool,
        "default": True,
        "description": "Allow fallback to another tracer backend when the preferred backend fails",
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
    "composites.run.enabled": {
        "type": bool,
        "default": True,
        "description": "Enable local composite materialization during roar run",
    },
    "composites.run.min_confidence": {
        "type": float,
        "default": 0.80,
        "description": "Minimum dataset confidence for run-time composite materialization",
    },
    "composites.run.min_components": {
        "type": int,
        "default": 2,
        "description": "Minimum leaf components required to materialize a run composite",
    },
    "composites.run.max_roots_per_job": {
        "type": int,
        "default": 4,
        "description": "Maximum composite roots materialized per run job",
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
    existing = find_roar_dir(start_dir)
    if existing is not None:
        return existing

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


def _format_toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return _format_toml_string(value)
    if isinstance(value, list):
        items = ", ".join(_format_toml_value(item) for item in value)
        return f"[{items}]"
    return str(value)


def _emit_toml_table(
    lines: list[str],
    table_path: str,
    table_data: dict[str, Any],
    default_data: dict[str, Any],
) -> None:
    if not isinstance(table_data, dict):
        return

    scalar_items: list[tuple[str, Any]] = []
    nested_tables: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    array_tables: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []

    for key, value in table_data.items():
        default_value = default_data.get(key)
        if value == default_value:
            continue

        if isinstance(value, dict):
            nested_tables.append(
                (key, value, default_value if isinstance(default_value, dict) else {})
            )
            continue

        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            if isinstance(default_value, list) and all(
                isinstance(item, dict) for item in default_value
            ):
                default_array = default_value
            else:
                default_array = []
            array_tables.append(
                (
                    key,
                    cast(list[dict[str, Any]], value),
                    cast(list[dict[str, Any]], default_array),
                )
            )
            continue

        if value is None:
            continue
        scalar_items.append((key, value))

    if scalar_items:
        lines.append(f"[{table_path}]")
        for key, value in scalar_items:
            lines.append(f"{key} = {_format_toml_value(value)}")
        lines.append("")

    for key, nested_value, nested_defaults in nested_tables:
        _emit_toml_table(lines, f"{table_path}.{key}", nested_value, nested_defaults)

    for key, array_values, array_defaults in array_tables:
        if array_values == array_defaults or not array_values:
            continue
        for item in array_values:
            lines.append(f"[[{table_path}.{key}]]")
            for item_key, item_value in item.items():
                if item_value is None:
                    continue
                lines.append(f"{item_key} = {_format_toml_value(item_value)}")
            lines.append("")


def save_config(config: dict, config_path: Path):
    """
    Save configuration to a .roar.toml file.

    Only saves non-default values.
    """
    lines: list[str] = []
    defaults = _get_default_config()
    section_order = list(defaults.keys()) + [key for key in config if key not in defaults]

    for section in section_order:
        section_data = config.get(section)
        if not isinstance(section_data, dict):
            continue
        section_defaults = defaults.get(section, {})
        if not isinstance(section_defaults, dict):
            section_defaults = {}
        _emit_toml_table(lines, section, section_data, section_defaults)

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
    elif key_info["type"] is int:  # type: ignore[index]
        try:
            typed_value = int(value)
        except ValueError as exc:
            raise ValueError(f"Invalid integer value: {value}") from exc
    elif key_info["type"] is float:  # type: ignore[index]
        try:
            typed_value = float(value)
        except ValueError as exc:
            raise ValueError(f"Invalid float value: {value}") from exc
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
    elif key == "tracer.default":
        if value not in VALID_TRACER_MODES:
            raise ValueError(
                f"Invalid tracer mode: {value}. "
                f"Valid modes: {', '.join(sorted(VALID_TRACER_MODES))}"
            )
        typed_value = value
    else:
        typed_value = value

    if key == "composites.run.min_confidence" and not (0.0 <= float(typed_value) <= 1.0):
        raise ValueError("composites.run.min_confidence must be between 0.0 and 1.0")
    if key == "composites.run.min_components" and int(typed_value) < 2:
        raise ValueError("composites.run.min_components must be >= 2")
    if key == "composites.run.max_roots_per_job" and int(typed_value) < 1:
        raise ValueError("composites.run.max_roots_per_job must be >= 1")

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
