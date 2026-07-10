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
    run = subprocess.run(
        [sys.executable, "-m", "roar", "run", "--tracer", tracer, *command],
        check=False,
    )
    return run.returncode


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
    completion_index = (
        str(env.get("JOB_COMPLETION_INDEX") or "").strip()
        or str(env.get("PET_NODE_RANK") or "").strip()
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
        from roar.execution.fragments.transport import emit_fragment_dicts

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

        fragments = [item for item in payload.get("fragments", []) if isinstance(item, dict)]
        completion_index = task_id.split(":")[2] if task_id.count(":") >= 3 else "0"
        restart_attempt = task_id.split(":")[3] if task_id.count(":") >= 3 else "0"
        for fragment in fragments:
            metadata = fragment.setdefault("backend_metadata", {})
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

        result = emit_fragment_dicts(fragments)
        print(f"[roar-k8s] lineage emit: {result} ({len(fragments)} fragment(s))")
    except Exception as exc:
        print(f"[roar-k8s] warning: lineage emit failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
