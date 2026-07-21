from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

ScopeMode = Literal["anonymous", "private", "public", "project"]

VALID_SCOPE_MODES: set[str] = {"anonymous", "private", "public", "project"}


@dataclass(frozen=True)
class RepoScope:
    mode: ScopeMode
    source: Literal["scope", "legacy_treqs"]
    owner_id: str | None = None
    owner_type: str | None = None
    project_id: str | None = None
    visibility: str | None = None


def load_repo_scope(start_dir: str | Path | None = None) -> RepoScope | None:
    """Load the durable repo scope, falling back to legacy [treqs] binding."""
    data = _load_raw_config(start_dir)

    scope = data.get("scope")
    if isinstance(scope, dict):
        mode = _optional_string(scope.get("mode"))
        if mode in VALID_SCOPE_MODES:
            if mode == "project":
                binding = _repo_scope_from_treqs(data.get("treqs"))
                if binding is not None:
                    return RepoScope(
                        mode="project",
                        source="scope",
                        owner_id=binding.owner_id,
                        owner_type=binding.owner_type,
                        project_id=binding.project_id,
                        visibility=binding.visibility,
                    )
            else:
                return RepoScope(mode=mode, source="scope")  # type: ignore[arg-type]

    return _repo_scope_from_treqs(data.get("treqs"))


def save_repo_scope(mode: ScopeMode, start_dir: str | Path | None = None) -> Path:
    from .integrations.config import get_config_path_for_write

    config_path = get_config_path_for_write(str(start_dir) if start_dir is not None else None)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_upsert_scope_mode(text, mode), encoding="utf-8")
    return config_path


def clear_repo_scope(start_dir: str | Path | None = None) -> Path:
    from .integrations.config import get_config_path_for_write

    config_path = get_config_path_for_write(str(start_dir) if start_dir is not None else None)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("", encoding="utf-8")
        return config_path

    config_path.write_text(
        _remove_table(config_path.read_text(encoding="utf-8"), "scope"), encoding="utf-8"
    )
    return config_path


def _repo_scope_from_treqs(value: Any) -> RepoScope | None:
    if not isinstance(value, dict):
        return None

    owner_id = _optional_string(value.get("owner_id"))
    owner_type = _optional_string(value.get("owner_type"))
    project_id = _optional_string(value.get("project_id"))
    visibility = _optional_string(value.get("visibility"))
    if owner_id is None or owner_type not in {"user", "organization"}:
        return None

    return RepoScope(
        mode="project",
        source="legacy_treqs",
        owner_id=owner_id,
        owner_type=owner_type,
        project_id=project_id,
        visibility=visibility,
    )


def _load_raw_config(start_dir: str | Path | None) -> dict[str, Any]:
    from .integrations.config.loader import find_config_file

    config_path = find_config_file(str(start_dir) if start_dir is not None else None)
    if config_path is None:
        return {}

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}

    if config_path.name == "pyproject.toml":
        tool = data.get("tool")
        if isinstance(tool, dict):
            roar = tool.get("roar")
            return roar if isinstance(roar, dict) else {}
        return {}

    return data if isinstance(data, dict) else {}


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _upsert_scope_mode(text: str, mode: ScopeMode) -> str:
    without_scope = _remove_table(text, "scope").rstrip()
    prefix = f"{without_scope}\n\n" if without_scope else ""
    return f'{prefix}[scope]\nmode = "{mode}"\n'


def _remove_table(text: str, table_name: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    header_pattern = re.compile(r"^\s*\[(?P<table>[^\]]+)\]\s*(?:#.*)?$")
    array_header_pattern = re.compile(r"^\s*\[\[(?P<table>[^\]]+)\]\]\s*(?:#.*)?$")

    for line in lines:
        header = header_pattern.match(line)
        array_header = array_header_pattern.match(line)
        matched_table = None
        if header is not None:
            matched_table = header.group("table").strip()
        elif array_header is not None:
            matched_table = array_header.group("table").strip()

        if matched_table is not None:
            skipping = matched_table == table_name
            if skipping:
                continue

        if not skipping:
            output.append(line)

    return "\n".join(output).rstrip() + ("\n" if output else "")
