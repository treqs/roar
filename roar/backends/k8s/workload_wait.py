"""Shared kubectl helpers for workload status polling and retrieval."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

from roar.backends.k8s.manifest import (
    WORKLOAD_FAILURE_CONDITIONS,
    WORKLOAD_SUCCESS_CONDITIONS,
)


def extract_kubectl_global_flags(command: list[str]) -> list[str]:
    """Pull connection flags out of a kubectl command for reuse in follow-ups."""
    flags: list[str] = []
    for index, arg in enumerate(command):
        if arg in ("--context", "--kubeconfig", "--cluster", "--user") and index + 1 < len(command):
            flags.extend([arg, command[index + 1]])
        elif arg.startswith(("--context=", "--kubeconfig=", "--cluster=", "--user=")):
            flags.append(arg)
    return flags


def get_workload_document(
    *,
    kubectl_binary: str,
    global_flags: list[str],
    kubectl_resource: str,
    name: str,
    namespace: str,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch a workload object as JSON; returns (document, error)."""
    command = [
        kubectl_binary,
        *global_flags,
        "get",
        f"{kubectl_resource}/{name}",
        "-n",
        namespace,
        "-o",
        "json",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None, result.stderr.strip()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid kubectl JSON output: {exc}"
    return (payload, "") if isinstance(payload, dict) else (None, "unexpected kubectl output")


def terminal_condition(document: dict[str, Any]) -> tuple[bool | None, str]:
    """Return (succeeded, message) from workload status conditions.

    ``succeeded`` is None while the workload is still running. Condition
    type names are unioned across Job/JobSet/PyTorchJob/TrainJob.
    """
    status = document.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    for condition in conditions or []:
        if not isinstance(condition, dict) or condition.get("status") != "True":
            continue
        condition_type = str(condition.get("type") or "")
        if condition_type in WORKLOAD_SUCCESS_CONDITIONS:
            return True, condition_type
        if condition_type in WORKLOAD_FAILURE_CONDITIONS:
            return False, str(condition.get("message") or condition_type)
    return None, ""


def wait_for_workload_completion(
    *,
    kubectl_binary: str,
    global_flags: list[str],
    kubectl_resource: str,
    name: str,
    namespace: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> tuple[bool, dict[str, Any]]:
    print(
        f"[roar-k8s] waiting for {kubectl_resource}/{name} in namespace "
        f"{namespace} (timeout {timeout_seconds}s)"
    )
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        document, error = get_workload_document(
            kubectl_binary=kubectl_binary,
            global_flags=global_flags,
            kubectl_resource=kubectl_resource,
            name=name,
            namespace=namespace,
        )
        if document is not None:
            succeeded, message = terminal_condition(document)
            if succeeded is True:
                print(f"[roar-k8s] {kubectl_resource}/{name} completed")
                return True, {"terminal_condition": message}
            if succeeded is False:
                print(
                    f"[roar-k8s] {kubectl_resource}/{name} failed: {message}",
                    file=sys.stderr,
                )
                return False, {"terminal_condition": "Failed", "message": message}
        elif error:
            last_error = error
        time.sleep(poll_interval_seconds)

    message = f"timed out after {timeout_seconds}s"
    if last_error:
        message = f"{message}; last kubectl error: {last_error}"
    print(f"[roar-k8s] wait for {kubectl_resource}/{name} {message}", file=sys.stderr)
    return False, {"terminal_condition": None, "message": message}


__all__ = [
    "extract_kubectl_global_flags",
    "get_workload_document",
    "terminal_condition",
    "wait_for_workload_completion",
]
