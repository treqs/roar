from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_OSMO_TEMPLATE_SENTINEL_RE = re.compile(r"__ROAR_OSMO_TEMPLATE_([A-Za-z0-9_.-]+)__")


class _LiteralBlockString(str):
    """Marker for YAML literal-block string rendering."""


def _represent_literal_block_string(
    dumper: yaml.SafeDumper,
    data: _LiteralBlockString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.SafeDumper.add_representer(_LiteralBlockString, _represent_literal_block_string)


@dataclass(frozen=True)
class PreparedOsmoWorkflow:
    input_path: str
    output_path: str
    selected_tasks: list[str]
    modified_tasks: list[str]
    wrapped_tasks: list[str]
    lineage_dataset_name: str
    lineage_bundle_filename: str
    wrapper_script_path: str | None = None
    runtime_bundle_local_path: str | None = None
    runtime_bundle_remote_path: str | None = None
    runtime_install_requirement: str | None = None
    runtime_install_local_path: str | None = None
    runtime_install_remote_path: str | None = None


def prepare_osmo_workflow_for_lineage(
    *,
    input_path: Path,
    output_path: Path,
    lineage_dataset_name: str,
    lineage_bundle_filename: str,
    task_names: list[str] | None = None,
    default_to_all_tasks: bool = False,
    inject_runtime_wrapper: bool = False,
    wrapper_script_path: str = "/tmp/roar-osmo-wrapper.sh",
    runtime_bundle_local_path: str | None = None,
    runtime_bundle_remote_path: str = "/tmp/roar-osmo-runtime.tar.gz",
    runtime_install_requirement: str | None = None,
    runtime_install_local_path: str | None = None,
    runtime_install_remote_path: str = "/tmp/roar-osmo-install.whl",
) -> PreparedOsmoWorkflow:
    if runtime_bundle_local_path and not inject_runtime_wrapper:
        raise ValueError("runtime bundle staging requires --inject-runtime-wrapper")
    if runtime_install_requirement and not inject_runtime_wrapper:
        raise ValueError("runtime install requires --inject-runtime-wrapper")
    if runtime_install_local_path and not inject_runtime_wrapper:
        raise ValueError("runtime install artifact requires --inject-runtime-wrapper")
    if runtime_install_requirement and runtime_install_local_path:
        raise ValueError("runtime install requirement and artifact are mutually exclusive")

    payload = _load_workflow_payload(input_path)
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError(f"{input_path} is missing a workflow mapping")

    tasks = workflow.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{input_path} is missing workflow.tasks")

    selected_tasks = _select_target_tasks(tasks, task_names, default_to_all_tasks)
    modified_tasks: list[str] = []
    wrapped_tasks: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or "").strip()
        if task_name not in selected_tasks:
            continue
        task_modified = _ensure_lineage_dataset_output(
            task,
            lineage_dataset_name=lineage_dataset_name,
            lineage_bundle_filename=lineage_bundle_filename,
        )
        if inject_runtime_wrapper and _inject_runtime_wrapper(
            task,
            task_name=task_name,
            lineage_bundle_filename=lineage_bundle_filename,
            wrapper_script_path=wrapper_script_path,
            runtime_bundle_remote_path=runtime_bundle_remote_path
            if runtime_bundle_local_path
            else None,
            runtime_install_source=(
                runtime_install_remote_path
                if runtime_install_local_path
                else runtime_install_requirement
            ),
        ):
            task_modified = True
            wrapped_tasks.append(task_name)
        if runtime_bundle_local_path:
            _ensure_runtime_bundle_file(
                task,
                runtime_bundle_local_path=runtime_bundle_local_path,
                runtime_bundle_remote_path=runtime_bundle_remote_path,
            )
            task_modified = True
        if runtime_install_local_path:
            _ensure_runtime_install_file(
                task,
                runtime_install_local_path=runtime_install_local_path,
                runtime_install_remote_path=runtime_install_remote_path,
            )
            task_modified = True
        if task_modified:
            modified_tasks.append(task_name)

    rendered = _render_workflow_payload(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    return PreparedOsmoWorkflow(
        input_path=str(input_path),
        output_path=str(output_path),
        selected_tasks=selected_tasks,
        modified_tasks=modified_tasks,
        wrapped_tasks=wrapped_tasks,
        lineage_dataset_name=lineage_dataset_name,
        lineage_bundle_filename=lineage_bundle_filename,
        wrapper_script_path=wrapper_script_path if inject_runtime_wrapper else None,
        runtime_bundle_local_path=runtime_bundle_local_path,
        runtime_bundle_remote_path=runtime_bundle_remote_path
        if runtime_bundle_local_path
        else None,
        runtime_install_requirement=runtime_install_requirement,
        runtime_install_local_path=runtime_install_local_path,
        runtime_install_remote_path=runtime_install_remote_path
        if runtime_install_local_path
        else None,
    )


def _load_workflow_payload(input_path: Path) -> dict[str, Any]:
    raw_text = input_path.read_text(encoding="utf-8")
    normalized_text = re.sub(
        r":\s*{{\s*([A-Za-z0-9_.-]+)\s*}}(\s*(#.*)?)$",
        r': "__ROAR_OSMO_TEMPLATE_\1__"\2',
        raw_text,
        flags=re.MULTILINE,
    )
    payload = yaml.safe_load(normalized_text)
    if not isinstance(payload, dict):
        raise ValueError(f"{input_path} did not parse to a workflow mapping")
    return payload


def _select_target_tasks(
    tasks: list[Any],
    requested_task_names: list[str] | None,
    default_to_all_tasks: bool,
) -> list[str]:
    available_task_names = [
        str(task.get("name") or "").strip()
        for task in tasks
        if isinstance(task, dict) and str(task.get("name") or "").strip()
    ]
    if not available_task_names:
        raise ValueError("workflow.tasks does not contain any named tasks")

    if requested_task_names:
        normalized = [str(item).strip() for item in requested_task_names if str(item).strip()]
        if not normalized:
            raise ValueError("at least one non-empty task name is required")
        missing = [name for name in normalized if name not in available_task_names]
        if missing:
            raise ValueError(f"workflow is missing requested task(s): {', '.join(missing)}")
        # Preserve first-seen order and deduplicate.
        ordered: list[str] = []
        seen: set[str] = set()
        for name in normalized:
            if name in seen:
                continue
            ordered.append(name)
            seen.add(name)
        return ordered

    if len(available_task_names) > 1 and not default_to_all_tasks:
        raise ValueError(
            "workflow has multiple tasks; specify --task for the task(s) that should emit Roar lineage"
        )

    return available_task_names


def _ensure_lineage_dataset_output(
    task: dict[str, Any],
    *,
    lineage_dataset_name: str,
    lineage_bundle_filename: str,
) -> bool:
    outputs = task.get("outputs")
    if not isinstance(outputs, list):
        outputs = []
        task["outputs"] = outputs

    for item in outputs:
        if not isinstance(item, dict):
            continue
        dataset = item.get("dataset")
        if not isinstance(dataset, dict):
            continue
        existing_name = str(dataset.get("name") or "").strip()
        existing_path = str(dataset.get("path") or "").strip()
        if existing_name == lineage_dataset_name and existing_path == lineage_bundle_filename:
            return False

    outputs.append(
        {
            "dataset": {
                "name": lineage_dataset_name,
                "path": lineage_bundle_filename,
            }
        }
    )
    return True


def _inject_runtime_wrapper(
    task: dict[str, Any],
    *,
    task_name: str,
    lineage_bundle_filename: str,
    wrapper_script_path: str,
    runtime_bundle_remote_path: str | None,
    runtime_install_source: str | None,
) -> bool:
    command = task.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(
            f"workflow task {task_name!r} must define command as a non-empty list to inject the Roar runtime wrapper"
        )
    args = task.get("args")
    if args is None:
        normalized_args: list[Any] = []
    elif isinstance(args, list):
        normalized_args = list(args)
    else:
        raise ValueError(
            f"workflow task {task_name!r} must define args as a list when injecting the Roar runtime wrapper"
        )

    if command == ["bash", wrapper_script_path]:
        _ensure_wrapper_file(
            task,
            wrapper_script_path=wrapper_script_path,
            runtime_bundle_remote_path=runtime_bundle_remote_path,
            runtime_install_source=runtime_install_source,
        )
        return False

    _ensure_wrapper_file(
        task,
        wrapper_script_path=wrapper_script_path,
        runtime_bundle_remote_path=runtime_bundle_remote_path,
        runtime_install_source=runtime_install_source,
    )
    task["command"] = ["bash", wrapper_script_path]
    task["args"] = [
        task_name,
        "{{output}}/" + lineage_bundle_filename,
        *[str(item) for item in command],
        *[str(item) for item in normalized_args],
    ]
    return True


def _ensure_wrapper_file(
    task: dict[str, Any],
    *,
    wrapper_script_path: str,
    runtime_bundle_remote_path: str | None,
    runtime_install_source: str | None,
) -> None:
    files = task.get("files")
    if not isinstance(files, list):
        files = []
        task["files"] = files

    for item in files:
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "").strip() != wrapper_script_path:
            continue
        item["contents"] = _runtime_wrapper_contents(
            runtime_bundle_remote_path,
            runtime_install_source,
        )
        item.pop("localpath", None)
        return

    files.append(
        {
            "path": wrapper_script_path,
            "contents": _runtime_wrapper_contents(
                runtime_bundle_remote_path,
                runtime_install_source,
            ),
        }
    )


def _ensure_runtime_bundle_file(
    task: dict[str, Any],
    *,
    runtime_bundle_local_path: str,
    runtime_bundle_remote_path: str,
) -> None:
    files = task.get("files")
    if not isinstance(files, list):
        files = []
        task["files"] = files

    for item in files:
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "").strip() != runtime_bundle_remote_path:
            continue
        item["localpath"] = runtime_bundle_local_path
        item.pop("contents", None)
        return

    files.append(
        {
            "localpath": runtime_bundle_local_path,
            "path": runtime_bundle_remote_path,
        }
    )


def _ensure_runtime_install_file(
    task: dict[str, Any],
    *,
    runtime_install_local_path: str,
    runtime_install_remote_path: str,
) -> None:
    files = task.get("files")
    if not isinstance(files, list):
        files = []
        task["files"] = files

    for item in files:
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "").strip() != runtime_install_remote_path:
            continue
        item["localpath"] = runtime_install_local_path
        item.pop("contents", None)
        return

    files.append(
        {
            "localpath": runtime_install_local_path,
            "path": runtime_install_remote_path,
        }
    )


def _runtime_wrapper_contents(
    runtime_bundle_remote_path: str | None,
    runtime_install_source: str | None,
) -> str:
    runtime_setup = ""
    if runtime_bundle_remote_path:
        runtime_setup = f"""
runtime_bundle="{runtime_bundle_remote_path}"
runtime_root="/tmp/roar-osmo-runtime"
if [ -f "$runtime_bundle" ]; then
  rm -rf "$runtime_root"
  mkdir -p "$runtime_root"
  tar -xzf "$runtime_bundle" -C "$runtime_root"
  export PYTHONPATH="$runtime_root/python:$runtime_root/python/site-packages:${{PYTHONPATH:-}}"
  export PATH="$runtime_root/bin:${{PATH:-}}"
fi
"""
    runtime_install = ""
    if runtime_install_source:
        escaped_requirement = (
            runtime_install_source.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        )
        runtime_install = f"""
install_root="/tmp/roar-osmo-python"
rm -rf "$install_root"
mkdir -p "$install_root"
if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
  pip_bootstrap_root="/tmp/roar-osmo-pip"
  export HOME="$pip_bootstrap_root/home"
  export PYTHONUSERBASE="$pip_bootstrap_root/userbase"
  mkdir -p "$HOME" "$PYTHONUSERBASE"
  if ! "$python_bin" -m ensurepip --user >/dev/null 2>&1; then
    echo "pip is not available and ensurepip bootstrap failed for Roar OSMO wrapper" >&2
    exit 127
  fi
  python_minor_version="$("$python_bin" - <<'PY'
import sys

print(f"python{{sys.version_info[0]}}.{{sys.version_info[1]}}")
PY
)"
  export PYTHONPATH="$PYTHONUSERBASE/lib/$python_minor_version/site-packages:${{PYTHONPATH:-}}"
  pip_command="$PYTHONUSERBASE/bin/pip3"
  if [ ! -x "$pip_command" ]; then
    echo "ensurepip bootstrap did not produce an executable pip3 for Roar OSMO wrapper" >&2
    exit 127
  fi
  "$pip_command" install --disable-pip-version-check --no-input --target "$install_root" "{escaped_requirement}"
else
  "$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "{escaped_requirement}"
fi
export PYTHONPATH="$install_root:${{PYTHONPATH:-}}"
"""

    return _LiteralBlockString(
        f"""#!/usr/bin/env bash
set -euo pipefail

task_name="$1"
bundle_path="$2"
shift 2

export ROAR_JOB_INSTRUMENTED=1
export ROAR_NO_TELEMETRY=1
export ROAR_EXECUTION_BACKEND=osmo
export ROAR_PROJECT_DIR="${{ROAR_PROJECT_DIR:-$PWD}}"
{runtime_setup}
python_bin="$(command -v python3 || command -v python)"
if [ -z "$python_bin" ]; then
  echo "python interpreter not found for Roar OSMO wrapper" >&2
  exit 127
fi
{runtime_install}
"$python_bin" - <<'PY'
from pathlib import Path

import roar
from roar.execution.runtime import tracer_backends

package_dir = Path(roar.__file__).resolve().parent
if tracer_backends.find_ptrace_tracer(package_dir) is None:
    raise SystemExit(
        "installed roar-cli distribution does not expose roar-tracer; use a packaged wheel with bundled binaries"
    )
PY
set +e
"$python_bin" -m roar run --tracer ptrace --no-tracer-fallback "$@"
command_status=$?
mkdir -p "$(dirname "$bundle_path")"
"$python_bin" -m roar osmo export-lineage-bundle "$bundle_path" --task-id "$task_name" --task-name "$task_name"
export_status=$?
set -e

if [ "$command_status" -ne 0 ]; then
  exit "$command_status"
fi

exit "$export_status"
"""
    )


def _render_workflow_payload(payload: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(payload, sort_keys=False)
    rendered = re.sub(
        r"(['\"])__ROAR_OSMO_TEMPLATE_([A-Za-z0-9_.-]+)__\1",
        lambda match: "{{ " + match.group(2) + " }}",
        rendered,
    )
    rendered = _OSMO_TEMPLATE_SENTINEL_RE.sub(
        lambda match: "{{ " + match.group(1) + " }}",
        rendered,
    )
    return rendered


def resolve_roar_install_requirement(explicit_requirement: str | None = None) -> str:
    if explicit_requirement and explicit_requirement.strip():
        return explicit_requirement.strip()

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


__all__ = [
    "PreparedOsmoWorkflow",
    "prepare_osmo_workflow_for_lineage",
    "resolve_roar_install_requirement",
]
