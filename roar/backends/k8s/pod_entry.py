"""In-pod entrypoint for roar-instrumented Kubernetes Jobs.

Invoked by the injected container wrapper as
``python3 -m roar.backends.k8s.pod_entry <original command...>`` after the
roar runtime has been installed. Runs the original command under ``roar
run``, then exports the recorded job as an execution fragment stamped
with the k8s identity contract and streams it through the shared
fragment transport.

Lineage is best-effort: the training command's exit code is always
propagated, and lineage failures only warn.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("[roar-k8s] no command to execute", file=sys.stderr)
        return 2

    exit_code = _run_traced(command)
    _emit_lineage_best_effort()
    return exit_code


def _run_traced(command: list[str]) -> int:
    workdir = Path(os.environ.get("ROAR_K8S_WORKDIR") or os.getcwd())
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        os.chdir(workdir)
    except OSError as exc:
        print(f"[roar-k8s] cannot use workdir {workdir}: {exc}", file=sys.stderr)
        return _run_uninstrumented(command)

    if not (workdir / ".roar").is_dir():
        init = subprocess.run(
            [sys.executable, "-m", "roar", "init", "-n"],
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            print(
                f"[roar-k8s] roar init failed; running uninstrumented:\n{init.stderr}",
                file=sys.stderr,
            )
            return _run_uninstrumented(command)

    tracer = str(os.environ.get("ROAR_K8S_TRACER") or "preload").strip() or "preload"
    child_env = dict(os.environ)
    # Activate the roar_inject.pth sitecustomize in every Python child so
    # the k8s backend's runtime-import hooks (botocore/aiobotocore object
    # I/O capture) install themselves; events land next to the local db.
    child_env.setdefault("ROAR_WRAP", "1")
    child_env.setdefault("ROAR_K8S_OBJECT_IO_FILE", str(_object_io_events_path()))
    report_path = _run_report_path()
    _remove_stale_report(report_path)
    child_env["ROAR_RUN_REPORT_FILE"] = str(report_path)
    run = subprocess.run(
        [sys.executable, "-m", "roar", "run", "--tracer", tracer, *command],
        env=child_env,
        check=False,
    )
    if run.returncode != 0 and _reported_setup_error(report_path):
        print(
            "[roar-k8s] roar run failed before launching the workload; running uninstrumented",
            file=sys.stderr,
        )
        return _run_uninstrumented(command)
    return run.returncode


def _object_io_events_path() -> Path:
    return Path.cwd() / ".roar" / "k8s-object-io.jsonl"


def _run_report_path() -> Path:
    return Path.cwd() / ".roar" / "k8s-run-report.json"


def _remove_stale_report(report_path: Path) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        report_path.unlink()


def _reported_setup_error(report_path: Path) -> bool:
    """True only when roar positively reported a pre-launch setup failure.

    A missing or unreadable report is ambiguous — roar may have crashed
    after the workload ran — so it must NOT trigger a rerun: double-running
    non-idempotent training is worse than a lost-lineage failure.
    """
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("setup_error"))


def _run_uninstrumented(command: list[str]) -> int:
    completed = subprocess.run(command, check=False)
    return completed.returncode


def task_identity_from_environment(environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Return ``(task_id, task_name)`` per the k8s identity contract.

    ``task_id`` = ``pod_uid:container:completion_index:restart_attempt`` —
    never pod names (reused across operator restarts) or ranks (unstable
    under elastic rendezvous).
    """
    env = os.environ if environ is None else environ
    pod_uid = str(env.get("ROAR_K8S_POD_UID") or "unknown-pod").strip() or "unknown-pod"
    container = str(env.get("ROAR_K8S_CONTAINER") or "main").strip() or "main"
    # Node-index chain: Indexed Job/JobSet completion index, then torchrun's
    # PET_NODE_RANK, then PyTorchJob v1's operator-injected pod-level RANK
    # (stable node rank there — process-level RANK is never read because
    # pod_entry runs above the launcher).
    completion_index = (
        str(env.get("JOB_COMPLETION_INDEX") or "").strip()
        or str(env.get("PET_NODE_RANK") or "").strip()
        or str(env.get("RANK") or "").strip()
        or "0"
    )
    restart_attempt = (
        str(env.get("ROAR_K8S_RESTART_ATTEMPT") or "").strip()
        or str(env.get("TORCHELASTIC_RESTART_COUNT") or "").strip()
        or "0"
    )
    task_id = f"{pod_uid}:{container}:{completion_index}:{restart_attempt}"
    task_name = (
        str(env.get("ROAR_K8S_TASK_NAME") or "").strip()
        or str(env.get("ROAR_K8S_JOB_NAME") or "").strip()
        or "k8s-task"
    )
    return task_id, task_name


def _emit_lineage_best_effort() -> None:
    try:
        from roar.execution.fragments.export import export_local_job_fragment_bundle

        task_id, task_name = task_identity_from_environment()
        parent_job_uid = str(os.environ.get("ROAR_K8S_PARENT_JOB_UID") or "").strip()

        with tempfile.TemporaryDirectory(prefix="roar-k8s-") as tmp:
            bundle_path = Path(tmp) / "roar-fragments.json"
            export_local_job_fragment_bundle(
                roar_dir=Path.cwd() / ".roar",
                output_path=bundle_path,
                backend_name="k8s",
                task_id=task_id,
                task_name=task_name,
                parent_job_uid=parent_job_uid,
                default_task_name="k8s-task",
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))

        from roar.backends.k8s.mount_map import MOUNT_MAP_ENV, parse_mount_map

        fragments = [item for item in payload.get("fragments", []) if isinstance(item, dict)]
        _augment_with_object_io(fragments)
        mount_map = parse_mount_map(os.environ.get(MOUNT_MAP_ENV))
        completion_index = task_id.split(":")[2] if task_id.count(":") >= 3 else "0"
        restart_attempt = task_id.split(":")[3] if task_id.count(":") >= 3 else "0"
        for fragment in fragments:
            metadata = fragment.setdefault("backend_metadata", {})
            if mount_map:
                # Recorded raw; reconstitution applies the rewrite so the
                # mapping used stays auditable next to the captured paths.
                metadata["k8s_mount_map"] = mount_map
            metadata.update(
                {
                    "k8s_namespace": os.environ.get("ROAR_K8S_NAMESPACE"),
                    "k8s_pod_name": os.environ.get("ROAR_K8S_POD_NAME"),
                    "k8s_pod_uid": os.environ.get("ROAR_K8S_POD_UID"),
                    "k8s_node_name": os.environ.get("ROAR_K8S_NODE_NAME"),
                    "k8s_container": os.environ.get("ROAR_K8S_CONTAINER"),
                    "k8s_job_name": os.environ.get("ROAR_K8S_JOB_NAME"),
                    "k8s_completion_index": completion_index,
                    "k8s_restart_attempt": restart_attempt,
                }
            )

        result = _emit_or_bundle(fragments)
        print(f"[roar-k8s] lineage emit: {result} ({len(fragments)} fragment(s))")
    except Exception as exc:
        print(f"[roar-k8s] warning: lineage emit failed: {exc}", file=sys.stderr)


def _augment_with_object_io(fragments: list[dict]) -> None:
    """Fold captured S3 events into the fragment's read/write refs."""
    from roar.backends.k8s.object_io import load_object_io_refs

    events_path = Path(os.environ.get("ROAR_K8S_OBJECT_IO_FILE") or _object_io_events_path())
    reads, writes = load_object_io_refs(events_path)

    proxy_reads, proxy_writes = _load_proxy_log_refs(
        os.environ.get("ROAR_K8S_PROXY_LOG"),
        seen_reads={ref["path"] for ref in reads},
        seen_writes={ref["path"] for ref in writes},
    )
    reads.extend(proxy_reads)
    writes.extend(proxy_writes)

    if not reads and not writes:
        return
    for fragment in fragments:
        fragment.setdefault("reads", []).extend(reads)
        fragment.setdefault("writes", []).extend(writes)


def _load_proxy_log_refs(
    log_path: str | None,
    *,
    seen_reads: set[str],
    seen_writes: set[str],
) -> tuple[list[dict], list[dict]]:
    """Parse the proxy sidecar's log into refs for hook-invisible clients.

    The in-process hooks are the primary S3 capture — proxy entries are
    only added for paths the hooks did not already record.
    """
    if not log_path:
        return [], []
    path = Path(log_path)
    if not path.is_file():
        return [], []

    try:
        from roar.execution.cluster.proxy import parse_log_line
    except Exception:
        return [], []

    write_ops = {"PutObject", "CompleteMultipartUpload", "CopyObject"}
    read_ops = {"GetObject"}
    reads: dict[str, dict] = {}
    writes: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], []
    for line in lines:
        entry = parse_log_line(line)
        if entry is None:
            continue
        if entry.operation in write_ops:
            mode_seen, bucket_refs = seen_writes, writes
        elif entry.operation in read_ops:
            mode_seen, bucket_refs = seen_reads, reads
        else:
            continue
        s3_path = f"s3://{entry.bucket}/{entry.key}"
        if s3_path in mode_seen:
            continue
        ref: dict = {
            "path": s3_path,
            "hash": entry.etag or None,
            "hash_algorithm": "etag" if entry.etag else "",
            "size": int(entry.size_bytes or 0),
            "capture_method": "proxy",
        }
        if entry.byte_ranges:
            ref["byte_ranges"] = entry.byte_ranges
        bucket_refs[s3_path] = ref
    return list(reads.values()), list(writes.values())


def _emit_or_bundle(fragments: list[dict]) -> str:
    """Stream fragments; fall back to a bundle file when GLaaS is unreachable.

    The reachability probe short-circuits the obviously-dark case without
    paying per-batch POST timeouts; emit_fragment_dicts reports "streamed"
    only when every batch was delivered, so mid-run streaming failures
    (partial or total) also land in the bundle fallback.
    """
    from roar.execution.fragments.transport import emit_fragment_dicts

    bundle_dir = str(os.environ.get("ROAR_K8S_BUNDLE_DIR") or "").strip()

    if bundle_dir and not _glaas_reachable():
        return _write_bundle(bundle_dir, fragments)

    result = emit_fragment_dicts(fragments)
    if result != "streamed" and bundle_dir:
        return _write_bundle(bundle_dir, fragments)
    return result


def _glaas_reachable() -> bool:
    import urllib.request

    glaas_url = str(os.environ.get("GLAAS_URL") or "").strip()
    if not glaas_url:
        return False
    try:
        request = urllib.request.Request(f"{glaas_url.rstrip('/')}/api/v1/health")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _write_bundle(bundle_dir: str, fragments: list[dict]) -> str:
    from roar.backends.k8s.bundles import write_fragment_bundle

    pod_name = str(os.environ.get("ROAR_K8S_POD_NAME") or "pod").strip() or "pod"
    container = str(os.environ.get("ROAR_K8S_CONTAINER") or "main").strip() or "main"
    # Attempt resolution mirrors task_identity_from_environment so the
    # bundle name and the fragment task_id agree on which attempt this is.
    attempt = (
        str(os.environ.get("ROAR_K8S_RESTART_ATTEMPT") or "").strip()
        or str(os.environ.get("TORCHELASTIC_RESTART_COUNT") or "").strip()
        or "0"
    )
    target = write_fragment_bundle(
        Path(bundle_dir), pod_name, fragments, container=container, attempt=attempt
    )
    print(f"[roar-k8s] GLaaS unavailable; wrote fragment bundle to {target}")
    return "bundled"


if __name__ == "__main__":
    sys.exit(main())
