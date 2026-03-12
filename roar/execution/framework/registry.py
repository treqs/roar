"""Canonical registry surface for distributed execution backends."""

from __future__ import annotations

import importlib
import os
import shlex
from collections.abc import Mapping, Sequence
from importlib import metadata as importlib_metadata

from roar.execution.framework.contract import DistributedExecutionBackend

_ENTRYPOINT_GROUP = "roar.execution_backends"
_BUILTIN_EXECUTION_BACKEND_MODULES = ("roar.backends.ray.plugin",)
_registered_execution_backends: list[DistributedExecutionBackend] = []
_execution_backends_discovered = False
_execution_backends_discovering = False


def register_execution_backend(backend: DistributedExecutionBackend) -> None:
    for existing in _registered_execution_backends:
        if existing.name == backend.name:
            return
    _registered_execution_backends.append(backend)


def iter_execution_backends() -> tuple[DistributedExecutionBackend, ...]:
    _ensure_execution_backends_discovered()
    return tuple(_registered_execution_backends)


def get_execution_backend(name: str) -> DistributedExecutionBackend:
    _ensure_execution_backends_discovered()
    normalized_name = str(name or "").strip()
    for backend in _registered_execution_backends:
        if backend.name == normalized_name:
            return backend
    raise LookupError(f"unknown execution backend: {normalized_name or '<empty>'}")


def match_execution_backend_for_module(module_name: str) -> DistributedExecutionBackend | None:
    _ensure_execution_backends_discovered()
    normalized_name = str(module_name or "").strip()
    if not normalized_name:
        return None

    for backend in _registered_execution_backends:
        adapter = backend.runtime_import
        if adapter is None:
            continue
        for prefix in adapter.module_prefixes:
            normalized_prefix = str(prefix or "").strip()
            if not normalized_prefix:
                continue
            if normalized_name == normalized_prefix or normalized_name.startswith(
                f"{normalized_prefix}."
            ):
                return backend
    return None


def iter_execution_noise_commands() -> tuple[str, ...]:
    _ensure_execution_backends_discovered()
    commands: list[str] = []
    for backend in _registered_execution_backends:
        policy = backend.policy
        if policy is None:
            continue
        commands.extend(str(command) for command in policy.noise_commands if str(command))
    return tuple(dict.fromkeys(commands))


def is_execution_noise_command(command: str | None) -> bool:
    text = str(command or "")
    return bool(text) and text in iter_execution_noise_commands()


def iter_execution_task_command_prefixes() -> tuple[str, ...]:
    _ensure_execution_backends_discovered()
    prefixes: list[str] = []
    for backend in _registered_execution_backends:
        policy = backend.policy
        if policy is None:
            continue
        prefixes.extend(str(prefix) for prefix in policy.task_command_prefixes if str(prefix))
    return tuple(dict.fromkeys(prefixes))


def is_execution_task_command(command: str | None) -> bool:
    text = str(command or "")
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in iter_execution_task_command_prefixes())


def iter_execution_job_environment_markers() -> tuple[str, ...]:
    _ensure_execution_backends_discovered()
    markers: list[str] = []
    for backend in _registered_execution_backends:
        policy = backend.policy
        if policy is None:
            continue
        markers.extend(str(marker) for marker in policy.job_environment_markers if str(marker))
    return tuple(dict.fromkeys(markers))


def is_execution_backend_job_environment(environ: Mapping[str, str] | None = None) -> bool:
    resolved_env = os.environ if environ is None else environ
    if _truthy_env_value(resolved_env.get("ROAR_JOB_INSTRUMENTED")):
        return True
    return any(marker in resolved_env for marker in iter_execution_job_environment_markers())


def is_execution_submit_command(command: str | Sequence[str] | None) -> bool:
    tokens = _normalize_command_tokens(command)
    if not tokens:
        return False
    return any(backend.matches_submit_command(list(tokens)) for backend in iter_execution_backends())


def _ensure_execution_backends_discovered() -> None:
    global _execution_backends_discovered, _execution_backends_discovering

    if _execution_backends_discovered or _execution_backends_discovering:
        return

    _execution_backends_discovering = True
    try:
        _load_builtin_execution_backends()
        _load_entrypoint_execution_backends()
        _execution_backends_discovered = True
    finally:
        _execution_backends_discovering = False


def _load_builtin_execution_backends() -> None:
    for module_name in _BUILTIN_EXECUTION_BACKEND_MODULES:
        importlib.import_module(module_name)


def _load_entrypoint_execution_backends() -> None:
    for entry_point in _iter_execution_backend_entrypoints():
        payload = entry_point.load()
        _register_entrypoint_payload(payload)


def _iter_execution_backend_entrypoints():
    try:
        return tuple(importlib_metadata.entry_points(group=_ENTRYPOINT_GROUP))
    except TypeError:
        entry_points = importlib_metadata.entry_points()
        select = getattr(entry_points, "select", None)
        if callable(select):
            return tuple(select(group=_ENTRYPOINT_GROUP))
        return tuple(entry_points.get(_ENTRYPOINT_GROUP, ()))


def _register_entrypoint_payload(payload: object) -> None:
    if isinstance(payload, DistributedExecutionBackend):
        register_execution_backend(payload)
        return

    if not callable(payload):
        raise TypeError(
            "execution backend entry point must load to a DistributedExecutionBackend or callable, "
            f"got {type(payload).__name__}"
        )

    result = payload()
    if result is None:
        return
    if isinstance(result, DistributedExecutionBackend):
        register_execution_backend(result)
        return
    if isinstance(result, (list, tuple)):
        for item in result:
            if not isinstance(item, DistributedExecutionBackend):
                raise TypeError(
                    "execution backend entry point iterable must contain "
                    "DistributedExecutionBackend items"
                )
            register_execution_backend(item)
        return

    raise TypeError(
        "execution backend entry point callable must return None, DistributedExecutionBackend, "
        f"or a list/tuple of backends, got {type(result).__name__}"
    )


def _normalize_command_tokens(command: str | Sequence[str] | None) -> tuple[str, ...]:
    if command is None:
        return ()
    if isinstance(command, str):
        text = command.strip()
        if not text:
            return ()
        try:
            return tuple(shlex.split(text))
        except ValueError:
            return tuple(part for part in text.split() if part)
    return tuple(str(part) for part in command if str(part))


def _truthy_env_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "get_execution_backend",
    "is_execution_backend_job_environment",
    "is_execution_noise_command",
    "is_execution_submit_command",
    "is_execution_task_command",
    "iter_execution_backends",
    "iter_execution_job_environment_markers",
    "iter_execution_noise_commands",
    "iter_execution_task_command_prefixes",
    "match_execution_backend_for_module",
    "register_execution_backend",
]
