"""kubectl Job submit planning through the execution backend framework."""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from roar.backends.k8s.config import load_k8s_backend_config
from roar.backends.k8s.manifest import (
    K8sManifestError,
    dump_manifest_documents,
    find_workload_documents,
    load_manifest_documents,
    rewrite_manifest_for_lineage,
)
from roar.execution.fragments.sessions import (
    generate_fragment_session,
    resolve_project_roar_dir,
    save_fragment_session,
)
from roar.execution.framework.contract import ExecutionCommandPlan

_KUBECTL_VERBS = ("apply", "create")
SUBMIT_CONTEXT_SUFFIX = ".context.json"


@dataclass(frozen=True)
class K8sSubmitContext:
    """Sidecar context linking a prepared manifest back to its submit."""

    original_command: list[str]
    manifest_path: str
    prepared_path: str
    workload_kind: str
    kubectl_resource: str
    job_name: str
    namespace: str
    secret_name: str
    session_id: str | None
    parent_job_uid: str
    wrapped_containers: list[str]
    skipped_containers: list[str]


def matches_kubectl_job_submit_command(command: list[str]) -> bool:
    if len(command) < 4:
        return False
    if Path(command[0]).name.lower() != "kubectl":
        return False
    # Global flags may precede the verb (kubectl --context X apply -f ...),
    # so accept the verb anywhere. The -f-points-at-a-manifest and
    # single-supported-workload guards below keep false positives out.
    if not any(arg.lower() in _KUBECTL_VERBS for arg in command[1:]):
        return False
    if not _k8s_backend_enabled():
        return False

    filename = _find_filename_argument(command)
    if filename is None:
        return False
    manifest_path = Path(filename[1])
    if not manifest_path.is_file():
        return False

    try:
        documents = load_manifest_documents(manifest_path)
    except K8sManifestError:
        return False
    return len(find_workload_documents(documents)) == 1


def plan_kubectl_job_submit_command(command: list[str]) -> ExecutionCommandPlan:
    if not matches_kubectl_job_submit_command(command):
        return ExecutionCommandPlan(backend_name="k8s", command=list(command))

    filename = _find_filename_argument(command)
    assert filename is not None
    _filename_index, manifest_arg = filename
    manifest_path = Path(manifest_arg).resolve()

    start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
    config = load_k8s_backend_config(start_dir=start_dir)
    roar_dir = resolve_project_roar_dir()

    glaas_url = _resolve_glaas_url()
    if glaas_url is None:
        _warn(
            "GLaaS is not configured; submitting the Job without lineage instrumentation "
            "(set glaas.url to enable fragment streaming)"
        )
        return ExecutionCommandPlan(
            backend_name="k8s",
            command=list(command),
            execution_role="submit",
        )

    session = generate_fragment_session()
    try:
        _register_fragment_session(
            glaas_url,
            session["session_id"],
            session["token_hash"],
            ttl=int(config.get("fragment_session_ttl_seconds", 86400)),
        )
    except Exception as exc:
        _warn(f"fragment session pre-registration failed ({exc}); submitting uninstrumented")
        return ExecutionCommandPlan(
            backend_name="k8s",
            command=list(command),
            execution_role="submit",
        )

    parent_job_uid = secrets.token_hex(4)
    secret_name = f"roar-fragment-{session['session_id'][:8]}"
    documents = load_manifest_documents(manifest_path)
    rewrite = rewrite_manifest_for_lineage(
        documents,
        secret_name=secret_name,
        session_id=session["session_id"],
        fragment_token=session["token"],
        requirement=resolve_runtime_requirement(config),
        cluster_glaas_url=_resolve_cluster_glaas_url(config, glaas_url),
        tracer=str(config.get("tracer") or "preload"),
        parent_job_uid=parent_job_uid,
        bundle_dir=str(config.get("bundle_dir") or ""),
        mount_map=_config_mount_map(config),
        runtime_source=str(config.get("runtime_source") or "install"),
        runtime_image=str(config.get("runtime_image") or ""),
        proxy_sidecar=bool(config.get("proxy_sidecar", False)),
        proxy_upstream=str(config.get("proxy_upstream") or ""),
        namespace_override=_find_namespace_argument(command),
    )
    if bool(config.get("proxy_sidecar", False)) and (
        str(config.get("runtime_source") or "install") != "image"
        or not str(config.get("runtime_image") or "")
    ):
        _warn(
            "k8s.proxy_sidecar requires k8s.runtime_source='image' with "
            "k8s.runtime_image set; sidecar not injected"
        )
    if rewrite.skipped_containers:
        _warn(
            "containers without an explicit command were left uninstrumented: "
            + ", ".join(rewrite.skipped_containers)
        )

    save_fragment_session(roar_dir, session)

    prepared_path = _write_prepared_manifest(
        roar_dir,
        rewrite.job_name,
        parent_job_uid,
        dump_manifest_documents(rewrite.documents),
    )
    submit_context = K8sSubmitContext(
        original_command=list(command),
        manifest_path=str(manifest_path),
        prepared_path=str(prepared_path),
        workload_kind=rewrite.workload_kind,
        kubectl_resource=rewrite.kubectl_resource,
        job_name=rewrite.job_name,
        namespace=rewrite.namespace,
        secret_name=secret_name,
        session_id=session["session_id"],
        parent_job_uid=parent_job_uid,
        wrapped_containers=list(rewrite.wrapped_containers),
        skipped_containers=list(rewrite.skipped_containers),
    )
    _write_submit_context(prepared_path, submit_context)

    rewritten_command = _replace_filename_argument(command, str(prepared_path))

    # RayJob pods stream Ray TaskFragments, so reconstitution is delegated
    # to the Ray backend's reconstituter rather than the k8s one.
    finalize_run = None
    if rewrite.workload_kind == "RayJob":
        from roar.execution.fragments.reconstitution import build_submit_finalizer

        finalize_run = build_submit_finalizer("ray", session["session_id"])

    return ExecutionCommandPlan(
        backend_name="k8s",
        command=rewritten_command,
        execution_role="submit",
        session_id=session["session_id"],
        finalize_run=finalize_run,
    )


def resolve_runtime_requirement(config: dict) -> str:
    import importlib.metadata as importlib_metadata

    override = os.environ.get("ROAR_CLUSTER_PIP_REQ", "").strip()
    if override:
        return override

    configured = str(config.get("runtime_install_requirement") or "").strip()
    if configured:
        return configured

    try:
        version = importlib_metadata.version("roar-cli")
        return f"roar-cli=={version}"
    except Exception:
        return "roar-cli"


def load_submit_context(prepared_path: Path) -> K8sSubmitContext | None:
    context_path = prepared_path.with_name(prepared_path.name + SUBMIT_CONTEXT_SUFFIX)
    if not context_path.is_file():
        return None
    try:
        payload = json.loads(context_path.read_text(encoding="utf-8"))
        return K8sSubmitContext(**payload)
    except Exception:
        return None


def discard_submit_context(prepared_path: Path) -> None:
    context_path = prepared_path.with_name(prepared_path.name + SUBMIT_CONTEXT_SUFFIX)
    context_path.unlink(missing_ok=True)


def _write_prepared_manifest(
    roar_dir: Path,
    job_name: str,
    parent_job_uid: str,
    contents: str,
) -> Path:
    prepared_dir = roar_dir / "k8s" / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = prepared_dir / f"{job_name}-{parent_job_uid}.yaml"
    # The prepared manifest embeds the fragment-session token in the Secret
    # document; keep it private and delete it right after kubectl reads it.
    fd = os.open(prepared_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(contents)
    return prepared_path


def _write_submit_context(prepared_path: Path, context: K8sSubmitContext) -> None:
    context_path = prepared_path.with_name(prepared_path.name + SUBMIT_CONTEXT_SUFFIX)
    context_path.write_text(json.dumps(asdict(context), indent=2) + "\n", encoding="utf-8")


def _k8s_backend_enabled() -> bool:
    start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
    return bool(load_k8s_backend_config(start_dir=start_dir).get("enabled", False))


def _find_filename_argument(command: list[str]) -> tuple[int, str] | None:
    for index, arg in enumerate(command[2:], start=2):
        if arg in ("-f", "--filename"):
            if index + 1 < len(command):
                return index + 1, command[index + 1]
            return None
        if arg.startswith("--filename="):
            return index, arg.split("=", 1)[1]
        if arg.startswith("-f="):
            return index, arg.split("=", 1)[1]
    return None


def _replace_filename_argument(command: list[str], prepared_path: str) -> list[str]:
    rewritten = list(command)
    location = _find_filename_argument(command)
    assert location is not None
    index, _value = location
    arg = rewritten[index]
    if arg.startswith("--filename="):
        rewritten[index] = f"--filename={prepared_path}"
    elif arg.startswith("-f="):
        rewritten[index] = f"-f={prepared_path}"
    else:
        rewritten[index] = prepared_path
    return rewritten


def _find_namespace_argument(command: list[str]) -> str | None:
    for index, arg in enumerate(command):
        if arg in ("-n", "--namespace") and index + 1 < len(command):
            return command[index + 1]
        if arg.startswith("--namespace="):
            return arg.split("=", 1)[1]
    return None


def _config_mount_map(config: dict) -> dict[str, str]:
    raw = config.get("mount_map")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if key and value}


def _resolve_glaas_url() -> str | None:
    from roar.integrations.glaas import get_glaas_url

    url = get_glaas_url()
    if url is None:
        return None
    text = str(url).strip()
    return text or None


def _resolve_cluster_glaas_url(config: dict, host_glaas_url: str) -> str:
    env_override = os.environ.get("ROAR_CLUSTER_GLAAS_URL", "").strip()
    if env_override:
        return env_override
    configured = str(config.get("cluster_glaas_url") or "").strip()
    if configured:
        return configured
    return host_glaas_url


def _register_fragment_session(
    glaas_url: str,
    session_id: str,
    token_hash: str,
    ttl: int = 86400,
) -> None:
    from roar.integrations.glaas import GlaasClient

    client = GlaasClient(base_url=glaas_url)
    _result, error = client.register_fragment_session(
        session_id=session_id,
        token_hash=token_hash,
        ttl_seconds=ttl,
    )
    if error:
        raise RuntimeError(f"failed to pre-register fragment session {session_id}: {error}")


def _warn(message: str) -> None:
    print(f"[roar-k8s] warning: {message}", file=sys.stderr)


__all__ = [
    "SUBMIT_CONTEXT_SUFFIX",
    "K8sSubmitContext",
    "discard_submit_context",
    "load_submit_context",
    "matches_kubectl_job_submit_command",
    "plan_kubectl_job_submit_command",
    "resolve_runtime_requirement",
]
