"""Canonical registry surface for execution backends."""

from __future__ import annotations

import importlib
import os
import shlex
from collections.abc import Mapping, Sequence
from importlib import metadata as importlib_metadata
from typing import Any

from roar.execution.framework.contract import BackendConfigAdapter, ExecutionBackend

_ENTRYPOINT_GROUP = "roar.execution_backends"
_BUILTIN_EXECUTION_BACKEND_MODULES = (
    "roar.backends.ray.plugin",
    "roar.backends.k8s.plugin",
    "roar.backends.osmo.plugin",
    "roar.backends.local.plugin",
)
# Builtin backend plugin modules skipped during discovery because their
# compiled deps failed to import (e.g. a wrong-ABI wheel under a cross-Python
# `roar run`). Maps module name -> import error string. Diagnostics only.
_skipped_builtin_backend_imports: dict[str, str] = {}
# Well-known job-environment markers for built-in backends. Each marker
# is also declared on its backend's ``ExecutionPolicyAdapter`` (single
# source of truth at the policy level — see consistency tests in
# ``tests/execution/framework/test_execution_planning.py``), but they're
# enumerated statically here so ``is_execution_backend_job_environment``
# can answer without triggering full backend discovery. That detection
# fires on *every* roar invocation (called from telemetry suppression),
# and triggering discovery would import every backend plugin module —
# ~300ms of cost in exchange for checking a handful of stable env-var
# names.
_BUILTIN_JOB_ENVIRONMENT_MARKERS: frozenset[str] = frozenset(
    {"RAY_JOB_ID", "ROAR_K8S_PARENT_JOB_UID"}
)

# Top-level TOML section names owned by built-in backends. The config
# loader checks against this to decide whether a `config_get("X.Y")`
# lookup *could* resolve through a backend (and so needs full
# backend-config resolution), or whether the raw TOML walk is
# authoritative. Without this static set, `config_get("hints.enabled")`
# would pay ~300ms loading every backend plugin just to confirm "hints"
# isn't a backend-namespaced key. Kept in sync with each builtin
# backend's ``BackendConfigAdapter.section_name`` by a consistency test.
_BUILTIN_BACKEND_CONFIG_SECTIONS: frozenset[str] = frozenset({"ray", "osmo", "k8s"})
_registered_execution_backends: list[ExecutionBackend] = []
_execution_backends_discovered = False
_execution_backends_discovering = False


def register_execution_backend(backend: ExecutionBackend) -> None:
    for existing in _registered_execution_backends:
        if existing.name == backend.name:
            return
    _registered_execution_backends.append(backend)


def iter_execution_backends() -> tuple[ExecutionBackend, ...]:
    _ensure_execution_backends_discovered()
    return tuple(
        sorted(
            _registered_execution_backends,
            key=lambda backend: backend.priority,
            reverse=True,
        )
    )


def get_execution_backend(name: str) -> ExecutionBackend:
    _ensure_execution_backends_discovered()
    normalized_name = str(name or "").strip()
    for backend in _registered_execution_backends:
        if backend.name == normalized_name:
            return backend

    # A miss with skipped builtin imports may be a transient early-import
    # failure, not a permanent one: sitecustomize (ROAR_WRAP=1) can trigger
    # discovery at interpreter startup before the runtime environment's
    # site-packages are fully importable (observed in Ray pip virtualenvs,
    # where roar's deps resolve fine moments later in the worker setup
    # hook). Retry the skipped imports once per lookup before giving up.
    if _skipped_builtin_backend_imports:
        _retry_skipped_builtin_backend_imports()
        for backend in _registered_execution_backends:
            if backend.name == normalized_name:
                return backend

    raise LookupError(f"unknown execution backend: {normalized_name or '<empty>'}")


def _retry_skipped_builtin_backend_imports() -> None:
    for module_name in list(_skipped_builtin_backend_imports):
        if module_name not in _BUILTIN_EXECUTION_BACKEND_MODULES:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            _skipped_builtin_backend_imports[module_name] = str(exc)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        try:
            _register_entrypoint_payload(register)
        except Exception as exc:
            _skipped_builtin_backend_imports[f"{module_name}:register"] = str(exc)
            continue
        _skipped_builtin_backend_imports.pop(module_name, None)
        _skipped_builtin_backend_imports.pop(f"{module_name}:register", None)


def match_execution_backend_for_module(module_name: str) -> ExecutionBackend | None:
    _ensure_execution_backends_discovered()
    normalized_name = str(module_name or "").strip()
    if not normalized_name:
        return None

    for backend in _registered_execution_backends:
        distributed = backend.distributed
        adapter = distributed.runtime_import if distributed is not None else None
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


def _normalize_job_backend_name(job: Mapping[str, Any] | None) -> str:
    if job is None:
        return ""
    return str(job.get("execution_backend") or "").strip()


def _normalize_job_execution_role(job: Mapping[str, Any] | None) -> str:
    if job is None:
        return ""
    return str(job.get("execution_role") or "").strip()


def _job_role_matches(job: Mapping[str, Any] | None, *, attr_name: str) -> bool:
    backend_name = _normalize_job_backend_name(job)
    execution_role = _normalize_job_execution_role(job)
    if not backend_name or not execution_role:
        return False

    try:
        backend = get_execution_backend(backend_name)
    except LookupError:
        return False

    policy = backend.policy
    if policy is None:
        return False

    allowed_roles = getattr(policy, attr_name)
    return execution_role in allowed_roles


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


def is_execution_noise_job(job: Mapping[str, Any] | None) -> bool:
    if _job_role_matches(job, attr_name="noise_roles"):
        return True
    return is_execution_noise_command(str((job or {}).get("command") or ""))


def is_execution_task_job(job: Mapping[str, Any] | None) -> bool:
    if _job_role_matches(job, attr_name="task_roles"):
        return True
    if is_execution_noise_job(job):
        return False
    return is_execution_task_command(str((job or {}).get("command") or ""))


def is_execution_phase_job(job: Mapping[str, Any] | None) -> bool:
    return _job_role_matches(job, attr_name="phase_roles")


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
    """Whether ``environ`` looks like a roar-spawned backend job.

    Fast path: well-known built-in markers (no backend discovery).
    Slow path (entry-point plugin backends only): consulted only if
    discovery has *already* happened in this process — never trigger
    discovery just for this check, since the caller is typically
    telemetry suppression running before any real command work.
    """
    resolved_env = os.environ if environ is None else environ
    if _truthy_env_value(resolved_env.get("ROAR_JOB_INSTRUMENTED")):
        return True
    if any(marker in resolved_env for marker in _BUILTIN_JOB_ENVIRONMENT_MARKERS):
        return True
    if not _execution_backends_discovered:
        return False
    return any(marker in resolved_env for marker in iter_execution_job_environment_markers())


def is_distributed_submission_command(command: str | Sequence[str] | None) -> bool:
    tokens = _normalize_command_tokens(command)
    if not tokens:
        return False
    return any(
        backend.distributed is not None and backend.matches_command(list(tokens))
        for backend in iter_execution_backends()
    )


def is_execution_submit_job(job: Mapping[str, Any] | None) -> bool:
    if _job_role_matches(job, attr_name="submit_roles"):
        return True
    return is_distributed_submission_command(str((job or {}).get("command") or ""))


def iter_execution_backend_config_adapters() -> tuple[BackendConfigAdapter, ...]:
    _ensure_execution_backends_discovered()
    adapters: list[BackendConfigAdapter] = []
    for backend in _registered_execution_backends:
        if backend.config is not None:
            adapters.append(backend.config)
    return tuple(adapters)


def iter_execution_backend_configurable_keys() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for adapter in iter_execution_backend_config_adapters():
        for key, spec in adapter.configurable_keys.items():
            merged[str(key)] = {
                "type": spec.value_type,
                "default": spec.default,
                "description": spec.description,
            }
    return merged


def iter_execution_backend_init_templates() -> tuple[str, ...]:
    templates: list[str] = []
    for adapter in iter_execution_backend_config_adapters():
        template = str(adapter.init_template or "").strip()
        if template:
            templates.append(template)
    return tuple(templates)


def resolve_execution_backend_config_sections(
    raw_config: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    payload = dict(raw_config or {})
    resolved: dict[str, dict[str, Any]] = {}
    for adapter in iter_execution_backend_config_adapters():
        raw_section = payload.get(adapter.section_name)
        mapping = raw_section if isinstance(raw_section, Mapping) else None
        if adapter.normalize_section is not None:
            resolved[adapter.section_name] = adapter.normalize_section(mapping)
            continue
        section = dict(adapter.default_values)
        if mapping is not None:
            section.update(dict(mapping))
        resolved[adapter.section_name] = section
    return resolved


def iter_execution_backend_section_names() -> tuple[str, ...]:
    return tuple(adapter.section_name for adapter in iter_execution_backend_config_adapters())


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
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            # A builtin backend whose compiled deps can't import — e.g. a
            # wrong-ABI wheel (pydantic_core, cryptography, ...) under a
            # cross-Python `roar run` — must not take down discovery for the
            # other backends or crash the traced workload. Skip it; it's simply
            # absent from the registry. Recorded for diagnostics/tests.
            _skipped_builtin_backend_imports[module_name] = str(exc)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError(
                "builtin execution backend module must export a callable register(), "
                f"missing on {module_name}"
            )
        _register_entrypoint_payload(register)


def _load_entrypoint_execution_backends() -> None:
    for entry_point in _iter_execution_backend_entrypoints():
        try:
            payload = entry_point.load()
        except ImportError as exc:
            # Same resilience as the builtin loader: an entry-point backend
            # whose compiled deps can't import (wrong-ABI wheel under a
            # cross-Python `roar run`) is skipped rather than crashing
            # discovery and the traced workload.
            _skipped_builtin_backend_imports[entry_point.value] = str(exc)
            continue
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
    if isinstance(payload, ExecutionBackend):
        register_execution_backend(payload)
        return

    if not callable(payload):
        raise TypeError(
            "execution backend entry point must load to an ExecutionBackend or callable, "
            f"got {type(payload).__name__}"
        )

    result = payload()
    if result is None:
        return
    if isinstance(result, ExecutionBackend):
        register_execution_backend(result)
        return
    if isinstance(result, (list, tuple)):
        for item in result:
            if not isinstance(item, ExecutionBackend):
                raise TypeError(
                    "execution backend entry point iterable must contain ExecutionBackend items"
                )
            register_execution_backend(item)
        return

    raise TypeError(
        "execution backend entry point callable must return None, ExecutionBackend, "
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
    "is_distributed_submission_command",
    "is_execution_backend_job_environment",
    "is_execution_noise_command",
    "is_execution_task_command",
    "iter_execution_backend_config_adapters",
    "iter_execution_backend_configurable_keys",
    "iter_execution_backend_init_templates",
    "iter_execution_backend_section_names",
    "iter_execution_backends",
    "iter_execution_job_environment_markers",
    "iter_execution_noise_commands",
    "iter_execution_task_command_prefixes",
    "match_execution_backend_for_module",
    "register_execution_backend",
    "resolve_execution_backend_config_sections",
]
