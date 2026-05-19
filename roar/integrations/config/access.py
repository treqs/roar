"""Configuration loading and management for roar."""

import re
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from ...core.tracer_modes import VALID_TRACER_MODES
from .loader import find_config_file, find_roar_dir, load_settings
from .raw import find_raw_config_file

try:
    import tomllib
except ImportError:
    import tomli as tomllib

# Valid hash algorithms
VALID_HASH_ALGORITHMS = {"blake3", "sha256", "sha512", "md5"}

# Config keys that can be set via `roar config`
CORE_CONFIGURABLE_KEYS: dict[str, dict[str, Any]] = {
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
        "description": (
            "Deprecated alias for output.verbosity. true → 'quiet'. Use output.verbosity instead."
        ),
    },
    "output.verbosity": {
        "type": str,
        "default": "normal",
        "valid_values": ("quiet", "normal", "verbose", "debug"),
        "description": (
            "Output level for `roar run`: "
            "quiet (silent), normal (status quo + filter counts), "
            "verbose (also list read/written files), "
            "debug (also list filtered files; can be large)"
        ),
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
    "glaas.query_nonlocal_ids_on_glaas": {
        "type": bool,
        "default": False,
        "description": "Allow read-only query commands to fall back to GLaaS for non-local artifact hashes",
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
    "registration.public_by_default": {
        "type": bool,
        "default": False,
        "description": "Default roar register/put to public publication intent unless overridden by --private",
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
    "tracer.banner": {
        "type": bool,
        "default": True,
        "description": "Show a one-time per-machine banner when a tracer backend is "
        "selected for the first time or via fallback (set to false to suppress)",
    },
    "runtime.install": {
        "type": str,
        "default": "auto",
        "description": (
            "How to handle a traced Python whose ABI doesn't match roar-cli's bundled "
            "deps. 'auto' = lazy-install a matching runtime tree on first roar run; "
            "'skip' = use bundled deps only (backend integrations disabled on mismatch). "
            "Useful in restricted-network containers."
        ),
    },
    "telemetry.enabled": {
        "type": bool,
        "default": True,
        "description": (
            "Enable anonymous product telemetry for this project unless disabled globally "
            "or by environment"
        ),
    },
    "telemetry.endpoint": {
        "type": str,
        "default": None,
        "description": (
            "Optional telemetry upload endpoint. When unset, roar derives it from glaas.url."
        ),
    },
    "git.remote": {
        "type": str,
        "default": None,
        "description": (
            "Canonical git remote roar pushes registration tags to. "
            "Falls back to the single remote when unset; required when multiple remotes exist."
        ),
    },
    "git.push_tags_on_register": {
        "type": str,
        "default": "auto",
        "valid_values": ("auto", "never"),
        "description": (
            "Whether `roar register` pushes roar tags to git.remote. "
            "'auto' pushes and fails-closed; 'never' skips and leaves GLaaS links unresolvable for teammates."
        ),
    },
    "hints.enabled": {
        "type": bool,
        "default": True,
        "description": (
            "Show advisory hint blocks (next steps, docs pointers, etc.) on "
            "stderr. Also auto-suppressed when output.verbosity = quiet."
        ),
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


def get_configurable_keys() -> dict[str, dict[str, Any]]:
    from roar.execution.framework.registry import iter_execution_backend_configurable_keys

    keys: dict[str, dict[str, Any]] = dict(CORE_CONFIGURABLE_KEYS)
    keys.update(iter_execution_backend_configurable_keys())
    return keys


class _ConfigurableKeysView(Mapping[str, dict[str, Any]]):
    def __getitem__(self, key: str) -> dict[str, Any]:
        return get_configurable_keys()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(get_configurable_keys())

    def __len__(self) -> int:
        return len(get_configurable_keys())

    def items(self):
        return get_configurable_keys().items()

    def keys(self):
        return get_configurable_keys().keys()

    def values(self):
        return get_configurable_keys().values()


CONFIGURABLE_KEYS: Mapping[str, dict[str, Any]] = _ConfigurableKeysView()


@dataclass(frozen=True)
class ConfigSetResult:
    config_path: Path
    typed_value: Any
    warnings: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Any]:
        yield self.config_path
        yield self.typed_value


def _get_default_config() -> dict:
    """Get default config from Pydantic models."""
    from .schema import RoarConfig

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
    current = d
    traversed: list[str] = []

    for part in parts[:-1]:
        traversed.append(part)
        next_value = current.get(part)
        if next_value is None:
            current[part] = {}
            next_value = current[part]
        elif not isinstance(next_value, dict):
            path = ".".join(traversed)
            raise ValueError(f"Cannot set {key}: {path} is not a table in config")
        current = next_value

    current[parts[-1]] = value


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


_MISSING = object()


def _build_nested_payload(key: str, value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    _set_nested(payload, key, value)
    return payload


def _load_raw_config_for_write(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse config file {config_path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Failed to read config file {config_path}: {exc}") from exc

    if config_path.name == "pyproject.toml":
        tool_data = data.get("tool", {}) if isinstance(data, dict) else {}
        if isinstance(tool_data, dict):
            roar_data = tool_data.get("roar", {})
            return roar_data if isinstance(roar_data, dict) else {}
        return {}

    return data if isinstance(data, dict) else {}


def _format_validation_location(location: tuple[Any, ...]) -> str:
    rendered = ""
    for part in location:
        if isinstance(part, int):
            rendered += f"[{part}]"
            continue
        part_text = str(part)
        rendered = f"{rendered}.{part_text}" if rendered else part_text
    return rendered


def _format_validation_message(error: Mapping[str, Any]) -> str:
    location = _format_validation_location(tuple(error.get("loc", ())))
    message = str(error.get("msg", "Invalid value"))
    if message.startswith("Value error, "):
        message = message[len("Value error, ") :]
    return f"{location}: {message}" if location else message


def _is_validation_error_for_key(location: tuple[Any, ...], key: str) -> bool:
    if not location:
        return False

    target = tuple(key.split("."))
    return location[: len(target)] == target or target[: len(location)] == location


def _collect_config_validation_messages(
    config: dict[str, Any],
    *,
    target_key: str,
) -> tuple[list[str], list[str]]:
    from .schema import RoarConfig

    try:
        RoarConfig.from_dict(config)
    except ValidationError as exc:
        targeted: list[str] = []
        unrelated: list[str] = []
        for error in exc.errors():
            message = _format_validation_message(error)
            location = tuple(error.get("loc", ()))
            if _is_validation_error_for_key(location, target_key):
                targeted.append(message)
            else:
                unrelated.append(message)
        return targeted, unrelated

    return [], []


def _validate_targeted_config_value(key: str, value: Any) -> Any:
    from .schema import RoarConfig

    try:
        config = RoarConfig.from_dict(_build_nested_payload(key, value))
    except ValidationError as exc:
        messages = [_format_validation_message(error) for error in exc.errors()]
        raise ValueError("; ".join(messages)) from exc

    validated = config.get(key, _MISSING)
    if validated is _MISSING:
        return value
    return _coerce_settings_value(validated)


def _coerce_settings_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return value


def _get_nested_from_settings(settings: Any, key: str, default: Any = _MISSING) -> Any:
    current: Any = settings
    for part in key.split("."):
        if hasattr(current, part):
            current = getattr(current, part)
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return default
    return _coerce_settings_value(current)


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


_TABLE_HEADER_RE = re.compile(r"^\s*\[(?P<path>[^\]]+)\]\s*(?:#.*)?$")
_ARRAY_TABLE_HEADER_RE = re.compile(r"^\s*\[\[(?P<path>[^\]]+)\]\]\s*(?:#.*)?$")


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


def _find_table_bounds(lines: list[str], table_path: str) -> tuple[int, int, int] | None:
    header_index: int | None = None
    content_start = 0

    for index, line in enumerate(lines):
        match = _TABLE_HEADER_RE.match(line)
        if match is None:
            continue
        if match.group("path").strip() != table_path:
            continue
        header_index = index
        content_start = index + 1
        break

    if header_index is None:
        return None

    end_index = len(lines)
    for index in range(content_start, len(lines)):
        if _TABLE_HEADER_RE.match(lines[index]) or _ARRAY_TABLE_HEADER_RE.match(lines[index]):
            end_index = index
            break

    return header_index, content_start, end_index


def _update_existing_config_text(text: str, key: str, value: Any) -> str:
    parts = key.split(".")
    table_path = ".".join(parts[:-1])
    field_name = parts[-1]
    rendered_value = _format_toml_value(value)
    lines = text.splitlines()

    bounds = _find_table_bounds(lines, table_path)
    if bounds is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend([f"[{table_path}]", f"{field_name} = {rendered_value}"])
        return "\n".join(lines) + "\n"

    _header_index, content_start, end_index = bounds
    assignment_pattern = re.compile(rf"^(?P<indent>\s*){re.escape(field_name)}\s*=")
    for index in range(content_start, end_index):
        match = assignment_pattern.match(lines[index])
        if match is None:
            continue
        indent = match.group("indent")
        lines[index] = f"{indent}{field_name} = {rendered_value}"
        return "\n".join(lines) + "\n"

    commented_assignment_pattern = re.compile(rf"^(?P<indent>\s*)#\s*{re.escape(field_name)}\s*=")
    for index in range(content_start, end_index):
        match = commented_assignment_pattern.match(lines[index])
        if match is None:
            continue
        indent = match.group("indent")
        lines[index] = f"{indent}{field_name} = {rendered_value}"
        return "\n".join(lines) + "\n"

    insert_index = end_index
    while insert_index > content_start and not lines[insert_index - 1].strip():
        insert_index -= 1
    lines.insert(insert_index, f"{field_name} = {rendered_value}")
    return "\n".join(lines) + "\n"


def _update_config_file_in_place(config_path: Path, key: str, value: Any) -> bool:
    if config_path.name != "config.toml" or not config_path.exists():
        return False

    original_text = config_path.read_text(encoding="utf-8")
    updated_text = _update_existing_config_text(original_text, key, value)
    config_path.write_text(updated_text, encoding="utf-8")
    return True


def _emit_toml_table(
    lines: list[str],
    table_path: str,
    table_data: dict[str, Any],
    default_data: dict[str, Any],
    preserve_data: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(table_data, dict):
        return

    scalar_items: list[tuple[str, Any]] = []
    nested_tables: list[tuple[str, dict[str, Any], dict[str, Any], Mapping[str, Any] | None]] = []
    array_tables: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]] = []

    for key, value in table_data.items():
        default_value = default_data.get(key)
        preserved_value = (
            preserve_data.get(key, _MISSING) if preserve_data is not None else _MISSING
        )
        preserve_key = preserved_value is not _MISSING

        if isinstance(value, dict):
            nested_tables.append(
                (
                    key,
                    value,
                    default_value if isinstance(default_value, dict) else {},
                    preserved_value if isinstance(preserved_value, Mapping) else None,
                )
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
                    preserve_key,
                )
            )
            continue

        if value is None:
            continue
        if value == default_value and not preserve_key:
            continue
        scalar_items.append((key, value))

    if scalar_items:
        lines.append(f"[{table_path}]")
        for key, value in scalar_items:
            lines.append(f"{key} = {_format_toml_value(value)}")
        lines.append("")

    for key, nested_value, nested_defaults, nested_preserve in nested_tables:
        _emit_toml_table(
            lines,
            f"{table_path}.{key}",
            nested_value,
            nested_defaults,
            nested_preserve,
        )

    for key, array_values, array_defaults, preserve_key in array_tables:
        if (array_values == array_defaults and not preserve_key) or not array_values:
            continue
        for item in array_values:
            lines.append(f"[[{table_path}.{key}]]")
            for item_key, item_value in item.items():
                if item_value is None:
                    continue
                lines.append(f"{item_key} = {_format_toml_value(item_value)}")
            lines.append("")


def save_config(
    config: dict,
    config_path: Path,
    *,
    preserve_existing: Mapping[str, Any] | None = None,
):
    """
    Save configuration to a .roar.toml file.

    Only saves non-default values unless they were already materialized.
    Preserves unknown top-level sections that are already present in the file.
    """
    existing_unknown_sections: dict[str, Any] = {}
    raw_path = find_raw_config_file(
        start_dir=str(
            config_path.parent.parent if config_path.name == "config.toml" else config_path.parent
        )
    )
    if raw_path and raw_path.exists() and raw_path.suffix == ".toml":
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            with raw_path.open("rb") as handle:
                raw_data = tomllib.load(handle)
            if raw_path.name == "pyproject.toml":
                raw_data = raw_data.get("tool", {}).get("roar", {})
            defaults = _get_default_config()
            existing_unknown_sections = {
                key: value
                for key, value in raw_data.items()
                if key not in defaults and key not in config
            }
        except Exception:
            existing_unknown_sections = {}

    merged_config = dict(config)
    merged_config.update(existing_unknown_sections)

    lines: list[str] = []
    defaults = _get_default_config()
    section_order = list(defaults.keys()) + [key for key in merged_config if key not in defaults]

    for section in section_order:
        section_data = merged_config.get(section)
        if not isinstance(section_data, dict):
            continue
        section_defaults = defaults.get(section, {})
        if not isinstance(section_defaults, dict):
            section_defaults = {}
        preserved_section = (
            preserve_existing.get(section)
            if preserve_existing is not None and isinstance(preserve_existing.get(section), Mapping)
            else None
        )
        _emit_toml_table(lines, section, section_data, section_defaults, preserved_section)

    config_path.write_text("\n".join(lines))


def config_get(key: str, start_dir: str | None = None):
    """Get a config value.

    Three-stage lookup, fast to slow:

      1. Typed pydantic sections (``output``, ``glaas``, …).
      2. Raw TOML data — covers user-set values in non-typed sections
         (``hints.enabled``, ad-hoc keys) without triggering backend-
         config resolution.
      3. Full ``to_dict()`` — only when the key prefix is a known
         backend section, since that's the only case where ``to_dict()``
         contributes defaults that the raw TOML doesn't. ``to_dict()``
         is the step that imports every backend plugin (~300ms); skipping
         it for ``hints.*`` and similar cheap reads is the bulk of the
         ``roar dag`` / ``roar show`` perf win.
    """
    settings = load_settings(start_dir=start_dir)
    value = _get_nested_from_settings(settings, key, _MISSING)
    if value is not _MISSING:
        return value

    raw_value = _get_nested(settings._backend_config_source, key)
    if raw_value is not None:
        return raw_value

    from ...execution.framework.registry import _BUILTIN_BACKEND_CONFIG_SECTIONS

    section = str(key).split(".", 1)[0]
    if section not in _BUILTIN_BACKEND_CONFIG_SECTIONS:
        return None

    config = settings.to_dict()
    return _get_nested(config, key)


def config_set(key: str, value: str, start_dir: str | None = None) -> ConfigSetResult:
    """Set a config value and save to .roar.toml."""
    configurable_keys = get_configurable_keys()
    if key not in configurable_keys:
        raise ValueError(
            f"Unknown config key: {key}. Valid keys: {', '.join(configurable_keys.keys())}"
        )

    key_info = configurable_keys[key]
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
    elif key == "output.verbosity":
        valid = key_info.get("valid_values", ())
        if value not in valid:
            raise ValueError(f"Invalid verbosity: {value}. Valid values: {', '.join(valid)}")
        typed_value = value
    elif key == "git.push_tags_on_register":
        valid = key_info.get("valid_values", ())
        if value not in valid:
            raise ValueError(
                f"Invalid git.push_tags_on_register: {value}. Valid values: {', '.join(valid)}"
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

    typed_value = _validate_targeted_config_value(key, typed_value)

    config_path = get_config_path_for_write(start_dir)
    raw_config = deepcopy(_load_raw_config_for_write(config_path))
    _set_nested(raw_config, key, typed_value)

    targeted_errors, warnings = _collect_config_validation_messages(raw_config, target_key=key)
    if targeted_errors:
        raise ValueError("; ".join(targeted_errors))

    if not _update_config_file_in_place(config_path, key, typed_value):
        save_config(raw_config, config_path, preserve_existing=raw_config)
    return ConfigSetResult(config_path, typed_value, tuple(warnings))


def config_list():
    """List all configurable keys with descriptions."""
    return get_configurable_keys()


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
