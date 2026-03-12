"""Canonical contract surface for execution backends."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from roar.cli.context import RoarContext
    from roar.services.execution.proxy import S3LogEntry


class FragmentReconstituterProtocol(Protocol):
    def reconstitute(self) -> Any: ...


SubmitCommandMatcher = Callable[[list[str]], bool]
SubmitCommandRewriter = Callable[[list[str]], "SubmitCommandRewrite"]
SubmitRunFinalizer = Callable[["RoarContext"], None]
DriverLocalMerge = Callable[[list[dict[str, Any]], str, str | None], None]
DriverProxyFragmentBuilder = Callable[
    [Sequence["S3LogEntry"], float, float, int, Mapping[str, str]],
    dict[str, Any] | None,
]
DriverLocalProxyPredicate = Callable[[Mapping[str, str]], bool]
WorkerRuntimeEnvPreparer = Callable[
    [Mapping[str, Any] | None, str, Mapping[str, str]], dict[str, Any]
]
WorkerBootstrapStartup = Callable[[], None]
WorkerEntrypointRunner = Callable[[list[str]], None]
ReconstituterFactory = Callable[[str, str, str, Path], FragmentReconstituterProtocol]
TrackedModulePatcher = Callable[[str, Any], None]
RuntimeProcessInitializer = Callable[[], None]
BackendConfigNormalizer = Callable[[Mapping[str, Any] | None], dict[str, Any]]

ROAR_EXECUTION_BACKEND_ENV = "ROAR_EXECUTION_BACKEND"


@dataclass(frozen=True)
class SubmitCommandRewrite:
    command: list[str]
    session_id: str | None = None
    finalize_run: SubmitRunFinalizer | None = None


@dataclass(frozen=True)
class DriverBootstrapAdapter:
    build_proxy_fragment: DriverProxyFragmentBuilder
    local_merge: DriverLocalMerge
    should_start_local_proxy: DriverLocalProxyPredicate | None = None


@dataclass(frozen=True)
class WorkerBootstrapAdapter:
    py_executable: str
    setup_hook: str
    prepare_runtime_env: WorkerRuntimeEnvPreparer
    startup: WorkerBootstrapStartup
    run_entrypoint: WorkerEntrypointRunner


@dataclass(frozen=True)
class FragmentReconstitutionAdapter:
    create_reconstituter: ReconstituterFactory


@dataclass(frozen=True)
class ExecutionPolicyAdapter:
    noise_commands: tuple[str, ...] = ()
    task_command_prefixes: tuple[str, ...] = ()
    job_environment_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeImportAdapter:
    module_prefixes: tuple[str, ...]
    initialize_process: RuntimeProcessInitializer | None = None
    observe_import: TrackedModulePatcher | None = None
    patch_module: TrackedModulePatcher | None = None


@dataclass(frozen=True)
class ConfigurableKeySpec:
    value_type: type[Any]
    default: Any
    description: str


@dataclass(frozen=True)
class BackendConfigAdapter:
    section_name: str
    default_values: Mapping[str, Any]
    configurable_keys: Mapping[str, ConfigurableKeySpec] = field(default_factory=dict)
    init_template: str = ""
    normalize_section: BackendConfigNormalizer | None = None


@dataclass(frozen=True)
class DistributedExecutionBackend:
    name: str
    matches_submit_command: SubmitCommandMatcher
    rewrite_submit_command: SubmitCommandRewriter
    driver_bootstrap: DriverBootstrapAdapter
    worker_bootstrap: WorkerBootstrapAdapter
    fragment_reconstitution: FragmentReconstitutionAdapter | None = None
    policy: ExecutionPolicyAdapter | None = None
    runtime_import: RuntimeImportAdapter | None = None
    config: BackendConfigAdapter | None = None


__all__ = [
    "ROAR_EXECUTION_BACKEND_ENV",
    "BackendConfigAdapter",
    "BackendConfigNormalizer",
    "ConfigurableKeySpec",
    "DistributedExecutionBackend",
    "DriverBootstrapAdapter",
    "ExecutionPolicyAdapter",
    "FragmentReconstitutionAdapter",
    "RuntimeImportAdapter",
    "SubmitCommandRewrite",
    "SubmitRunFinalizer",
    "WorkerBootstrapAdapter",
]
