"""Ray submit planning through the execution backend framework."""

from __future__ import annotations

import json
import os
from pathlib import Path

from roar.backends.ray.config import load_ray_backend_config
from roar.backends.ray.env_contract import merge_worker_bootstrap_env
from roar.backends.ray.submit_context import (
    RayInstrumentationContext,
    build_submit_instrumentation_context,
    build_submit_source_environ,
)
from roar.execution.fragments.sessions import (
    generate_fragment_session as generate_fragment_key,
)
from roar.execution.fragments.sessions import resolve_project_roar_dir
from roar.execution.fragments.sessions import save_fragment_session as save_key
from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV, ExecutionCommandPlan
from roar.integrations.glaas import GlaasClient

_ROAR_WORKER_SETUP_HOOK = "roar.execution.runtime.worker_bootstrap.startup"
_ROAR_DRIVER_ENTRYPOINT_MODULE = "roar.execution.runtime.driver_entrypoint"


def plan_ray_job_submit_command(command: list[str]) -> ExecutionCommandPlan:
    """Plan Ray job submit commands for Roar instrumentation."""
    if not matches_ray_job_submit_command(command):
        return ExecutionCommandPlan(backend_name="ray", command=command)

    if "--" not in command:
        return ExecutionCommandPlan(
            backend_name="ray",
            command=command,
            execution_role="submit",
        )

    separator_index = command.index("--")
    before_separator = list(command[:separator_index])
    entrypoint = list(command[separator_index + 1 :])
    if not entrypoint:
        return ExecutionCommandPlan(
            backend_name="ray",
            command=command,
            execution_role="submit",
        )
    entrypoint = _wrap_entrypoint_for_driver_proxy(entrypoint)

    runtime_env_json_arg = _find_runtime_env_json(before_separator)
    runtime_env = _load_runtime_env(before_separator, runtime_env_json_arg)
    if runtime_env is None:
        return ExecutionCommandPlan(
            backend_name="ray",
            command=command,
            execution_role="submit",
        )

    if _ray_pip_install_enabled():
        merged_pip = _merge_roar_runtime_env_pip(runtime_env.get("pip"))
        if merged_pip:
            runtime_env["pip"] = merged_pip
        else:
            runtime_env.pop("pip", None)

    runtime_env["worker_process_setup_hook"] = _ROAR_WORKER_SETUP_HOOK

    context = build_submit_instrumentation_context(
        os.environ,
        cwd=os.getcwd(),
        host_glaas_url=_resolve_glaas_url(),
    )
    env_vars = _build_instrumented_env_vars(
        existing_env_vars=dict(runtime_env.get("env_vars", {}) or {}),
        context=context,
    )
    fragment_session_id: str | None = None
    if context.endpoints.host_glaas_url:
        key = generate_fragment_key()
        try:
            _register_fragment_session(
                context.endpoints.host_glaas_url,
                key["session_id"],
                key["token_hash"],
            )
        except Exception:
            pass
        else:
            save_key(resolve_project_roar_dir(), key)
            env_vars["ROAR_SESSION_ID"] = key["session_id"]
            env_vars["ROAR_FRAGMENT_TOKEN"] = key["token"]
            fragment_session_id = str(key["session_id"])

    if env_vars:
        runtime_env["env_vars"] = env_vars
    else:
        runtime_env.pop("env_vars", None)

    before_separator = _store_runtime_env(before_separator, runtime_env, runtime_env_json_arg)
    return ExecutionCommandPlan(
        backend_name="ray",
        command=[*before_separator, "--", *entrypoint],
        execution_role="submit",
        session_id=fragment_session_id,
    )


def _build_instrumented_env_vars(
    *,
    existing_env_vars: dict[str, str],
    context: RayInstrumentationContext,
) -> dict[str, str]:
    env_vars = merge_worker_bootstrap_env(
        existing_env_vars,
        build_submit_source_environ(context),
        job_id=context.job_id,
        overwrite_existing=True,
    )
    env_vars[ROAR_EXECUTION_BACKEND_ENV] = "ray"
    env_vars["ROAR_WRAP"] = "1"
    return env_vars


def matches_ray_job_submit_command(command: list[str]) -> bool:
    if len(command) < 3:
        return False

    binary = Path(command[0]).name.lower()
    noun = command[1].lower()
    verb = command[2].lower()
    return binary == "ray" and noun in {"job", "jobs"} and verb == "submit"


def _wrap_entrypoint_for_driver_proxy(entrypoint: list[str]) -> list[str]:
    if (
        len(entrypoint) >= 3
        and entrypoint[1] == "-m"
        and entrypoint[2] == _ROAR_DRIVER_ENTRYPOINT_MODULE
    ):
        return entrypoint

    return ["python", "-m", _ROAR_DRIVER_ENTRYPOINT_MODULE, "--", *entrypoint]


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


def _merge_roar_runtime_env_pip(existing_pip: object) -> list[str] | None:
    roar_req = _resolve_roar_requirement()
    dependencies = _coerce_runtime_env_pip(existing_pip)
    dependencies = [
        dependency
        for dependency in dependencies
        if _requirement_name(dependency) not in {"roar-cli", "roar"}
        and dependency.strip() != roar_req.strip()
    ]
    if roar_req != "skip":
        dependencies.append(roar_req)
    return dependencies if dependencies else None


def _ray_pip_install_enabled() -> bool:
    start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
    return bool(load_ray_backend_config(start_dir=start_dir).get("pip_install", True))


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


def _resolve_roar_requirement() -> str:
    import importlib.metadata as importlib_metadata

    override = os.environ.get("ROAR_CLUSTER_PIP_REQ", "").strip()
    if override:
        return override

    try:
        version = importlib_metadata.version("roar-cli")
        return f"roar-cli=={version}"
    except importlib_metadata.PackageNotFoundError:
        pass
    except Exception:
        pass

    return "roar-cli"


def _resolve_glaas_url() -> str | None:
    from roar.integrations.glaas import get_glaas_url

    url = get_glaas_url()
    if url is None:
        return None

    text = str(url).strip()
    return text or None


def _register_fragment_session(
    glaas_url: str,
    session_id: str,
    token_hash: str,
    ttl: int = 86400,
) -> None:
    client = GlaasClient(base_url=glaas_url)
    _result, error = client.register_fragment_session(
        session_id=session_id,
        token_hash=token_hash,
        ttl_seconds=ttl,
    )
    if error:
        raise RuntimeError(f"failed to pre-register fragment session {session_id}: {error}")


__all__ = [
    "_merge_roar_runtime_env_pip",
    "_resolve_roar_requirement",
    "matches_ray_job_submit_command",
    "plan_ray_job_submit_command",
]
