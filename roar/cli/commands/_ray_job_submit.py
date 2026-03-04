"""Rewrites for `ray job(s) submit` launched through `roar run`."""

from __future__ import annotations

import json
from pathlib import Path


def maybe_rewrite_ray_job_submit(command: list[str]) -> list[str]:
    """Rewrite ray jobs submit commands for roar instrumentation."""
    if not _is_ray_job_submit(command):
        return command

    if "--" not in command:
        return command

    separator_index = command.index("--")
    before_separator = list(command[:separator_index])
    entrypoint = list(command[separator_index + 1 :])
    if not entrypoint:
        return command

    runtime_env_json_arg = _find_runtime_env_json(before_separator)
    runtime_env = _load_runtime_env(before_separator, runtime_env_json_arg)
    if runtime_env is None:
        # Invalid user-provided JSON - leave command untouched.
        return command

    merged_pip = _merge_roar_runtime_env_pip(runtime_env.get("pip"))
    if merged_pip:
        runtime_env["pip"] = merged_pip
    elif "pip" in runtime_env and merged_pip is not None:
        runtime_env["pip"] = merged_pip

    glaas_url = _resolve_glaas_url()
    if glaas_url:
        env_vars = dict(runtime_env.get("env_vars", {}) or {})
        env_vars["GLAAS_URL"] = glaas_url
        env_vars["GLAAS_API_URL"] = glaas_url
        runtime_env["env_vars"] = env_vars

    before_separator = _store_runtime_env(before_separator, runtime_env, runtime_env_json_arg)
    entrypoint = _wrap_entrypoint(entrypoint)
    return [*before_separator, "--", *entrypoint]


def _is_ray_job_submit(command: list[str]) -> bool:
    if len(command) < 3:
        return False

    binary = Path(command[0]).name.lower()
    noun = command[1].lower()
    verb = command[2].lower()
    return binary == "ray" and noun in {"job", "jobs"} and verb == "submit"


def _find_runtime_env_json(command: list[str]) -> tuple[int, int | None] | None:
    for index, arg in enumerate(command):
        if arg == "--runtime-env-json":
            if index + 1 < len(command):
                return index, index + 1
            return index, None
        if arg.startswith("--runtime-env-json="):
            return index, None
    return None


def _load_runtime_env(
    command_before_separator: list[str], runtime_env_json_arg: tuple[int, int | None] | None
) -> dict | None:
    if runtime_env_json_arg is None:
        return {}

    flag_index, value_index = runtime_env_json_arg
    if value_index is not None:
        payload = command_before_separator[value_index]
    else:
        payload = command_before_separator[flag_index].split("=", 1)[1]

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        return parsed
    return {}


def _store_runtime_env(
    command_before_separator: list[str],
    runtime_env: dict,
    runtime_env_json_arg: tuple[int, int | None] | None,
) -> list[str]:
    serialized = json.dumps(runtime_env, separators=(",", ":"))
    command_out = list(command_before_separator)

    if runtime_env_json_arg is None:
        command_out.extend(["--runtime-env-json", serialized])
        return command_out

    flag_index, value_index = runtime_env_json_arg
    if value_index is not None:
        command_out[value_index] = serialized
    else:
        command_out[flag_index] = f"--runtime-env-json={serialized}"
    return command_out


def _wrap_entrypoint(entrypoint: list[str]) -> list[str]:
    if len(entrypoint) >= 2 and Path(entrypoint[0]).name == "roar" and entrypoint[1] == "run":
        return entrypoint
    return ["roar", "run", *entrypoint]


def _merge_roar_runtime_env_pip(existing_pip: object) -> list[str] | None:
    roar_req = _resolve_roar_requirement()
    if roar_req is None:
        # Local dev mode: vendor wheel present means cluster has roar pre-installed.
        # Skip pip injection — preserve existing pip list unchanged.
        existing = _coerce_runtime_env_pip(existing_pip)
        return existing if existing else None
    dependencies = _coerce_runtime_env_pip(existing_pip)
    dependencies = [
        dependency
        for dependency in dependencies
        if _requirement_name(dependency) not in {"roar-cli", "roar"}
    ]
    dependencies.append(roar_req)
    return dependencies


def _coerce_runtime_env_pip(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _requirement_name(requirement: str) -> str:
    text = requirement.strip()
    if not text:
        return ""

    for delimiter in ("@", "==", ">=", "<=", "~=", "!=", ">", "<", ";", "["):
        position = text.find(delimiter)
        if position > 0:
            text = text[:position]
            break
    return text.strip().lower()


def _resolve_roar_requirement() -> str | None:
    import os
    wheel_path = Path(os.getcwd()) / "vendor" / "roar-cli.whl"
    if wheel_path.exists():
        # Local dev mode: vendor wheel exists, cluster has roar pre-installed.
        # Signal to skip pip injection entirely.
        return None

    import importlib.metadata as importlib_metadata

    for package_name in ("roar-cli", "roar"):
        try:
            return f"{package_name}=={importlib_metadata.version(package_name)}"
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:
            break
    return "roar-cli"


def _resolve_glaas_url() -> str | None:
    from ...glaas_client import get_glaas_url

    url = get_glaas_url()
    if not url:
        return None
    return str(url)
