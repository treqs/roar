"""System-managed label derivation and refresh helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from ..core.label_constants import AUTO_DATASET_LABEL_KEYS
from ..core.label_origins import LABEL_ORIGIN_SYSTEM
from ..db.context import optional_repo

JOB_SYSTEM_LABEL_ROOT = "roar"
# tag.*/attach.* are reserved for `roar tag`/`roar attach` — the namespace
# itself is the hereditary-propagation contract, so the generic `roar label`
# path must not be able to write there (see design-docs/20260519 roar
# audit.md, "Storage: no schema changes"). This also protects the tag.bind
# ledger's append-only integrity: TagService bypasses this check via its own
# direct label_repo writes (see application/tags.py).
SYSTEM_LABEL_ROOT_PREFIXES = frozenset({JOB_SYSTEM_LABEL_ROOT, "tag", "attach"})
SYSTEM_LABEL_EXACT_PATHS = frozenset(AUTO_DATASET_LABEL_KEYS)
DISPLAY_FILTER_ROOT_PREFIXES = frozenset({JOB_SYSTEM_LABEL_ROOT})

_OPERATION_KEYS = ("get", "put", "osmo_submit", "osmo_attach")


def is_reserved_system_label_path(path: str) -> bool:
    """Return True when a flattened label path is system-managed."""
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if normalized in SYSTEM_LABEL_EXACT_PATHS:
        return True
    return any(
        normalized == prefix or normalized.startswith(prefix + ".")
        for prefix in SYSTEM_LABEL_ROOT_PREFIXES
    )


def strip_reserved_system_labels(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a metadata copy with all reserved system label keys removed."""
    cleaned = json.loads(json.dumps(metadata))
    for path in SYSTEM_LABEL_EXACT_PATHS:
        _remove_nested(cleaned, path.split("."))
    for prefix in SYSTEM_LABEL_ROOT_PREFIXES:
        _remove_nested(cleaned, [prefix])
    return cleaned


def omit_display_system_labels(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Hide noisy system label roots from default user-facing renderers."""
    if not metadata:
        return metadata
    filtered = json.loads(json.dumps(metadata))
    for prefix in DISPLAY_FILTER_ROOT_PREFIXES:
        _remove_nested(filtered, [prefix])
    return filtered or None


def refresh_job_system_labels(
    db_ctx: object, *, job_id: int, job: Mapping[str, Any] | None = None
) -> None:
    """Refresh the reserved system label portion for one local job."""
    labels_repo = optional_repo(db_ctx, "labels")
    jobs_repo = optional_repo(db_ctx, "jobs")
    if labels_repo is None or jobs_repo is None:
        return

    job_row = dict(job) if job is not None else cast(Any, jobs_repo).get(job_id)
    if not isinstance(job_row, dict):
        return

    current = cast(Any, labels_repo).get_current("job", job_id=job_id)
    current_metadata = current.get("metadata") if isinstance(current, dict) else {}
    if not isinstance(current_metadata, dict):
        current_metadata = {}

    user_metadata = strip_reserved_system_labels(current_metadata)
    system_metadata = build_job_system_labels(job_row)
    merged = _deep_merge(user_metadata, system_metadata)
    if merged == current_metadata:
        return

    cast(Any, labels_repo).create_version(
        "job",
        merged,
        job_id=job_id,
        write_origin=LABEL_ORIGIN_SYSTEM,
    )


def build_job_system_labels(job: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the system-managed job label document from a local job row."""
    metadata = _normalize_metadata(job.get("metadata"))
    kind = _determine_operation_kind(job, metadata)

    roar: dict[str, Any] = {
        "schema_version": 1,
        "operation": {"kind": kind},
    }

    _populate_run_build_common_labels(roar, metadata)
    _populate_task_labels(roar, job, metadata)

    if kind == "get":
        _populate_get_labels(roar, metadata)
    elif kind == "put":
        _populate_put_labels(roar, metadata)
    elif kind in {"osmo_submit", "osmo_attach"}:
        _populate_osmo_labels(roar, metadata, kind=kind)

    return {JOB_SYSTEM_LABEL_ROOT: roar}


def _normalize_metadata(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _determine_operation_kind(job: Mapping[str, Any], metadata: dict[str, Any]) -> str:
    for key in _OPERATION_KEYS:
        if isinstance(metadata.get(key), dict):
            return key

    job_type = str(job.get("job_type") or "").strip().lower()
    if job_type == "build":
        return "build"
    if job_type in {"get", "put", "ray_task", "osmo_task"}:
        return job_type
    return "run"


def _populate_run_build_common_labels(roar: dict[str, Any], metadata: dict[str, Any]) -> None:
    # The producing roar version, frozen into job metadata at run-record time. Projected
    # here (never read live) so the label stays immutable across later re-derivation.
    _set_nested_value(roar, ["version"], metadata.get("roar_version"))

    git = metadata.get("git")
    if isinstance(git, dict):
        _set_nested_value(roar, ["git", "commit"], git.get("commit"))
        _set_nested_value(roar, ["git", "branch"], git.get("branch"))
        _set_nested_value(roar, ["git", "remote_url"], git.get("remote_url"))
        _set_nested_value(roar, ["git", "clean"], git.get("clean"))
        _set_nested_value(roar, ["git", "commit_timestamp"], git.get("commit_timestamp"))

    _set_nested_value(roar, ["cwd"], metadata.get("cwd"))

    runtime = metadata.get("runtime")
    if isinstance(runtime, dict):
        _set_nested_value(roar, ["runtime", "hostname"], runtime.get("hostname"))
        _copy_scalar_map(runtime.get("os"), roar, ["runtime", "os"])
        _copy_scalar_map(runtime.get("python"), roar, ["runtime", "python"])
        _copy_scalar_map(runtime.get("container"), roar, ["runtime", "container"])
        _copy_scalar_map(runtime.get("vm"), roar, ["runtime", "vm"])
        _copy_scalar_map(runtime.get("cuda"), roar, ["runtime", "cuda"])
        _copy_scalar_map(runtime.get("cpu"), roar, ["runtime", "cpu"])
        _copy_scalar_map(runtime.get("memory"), roar, ["runtime", "memory"])

        gpu = runtime.get("gpu")
        if isinstance(gpu, list) and gpu:
            _set_nested_value(roar, ["runtime", "gpu", "count"], len(gpu))
            for index, row in enumerate(gpu):
                if not isinstance(row, dict):
                    continue
                for key in ("name", "memory_mb", "compute_cap"):
                    _set_nested_value(
                        roar,
                        ["runtime", "gpu", str(index), key],
                        row.get(key),
                    )

    packages = metadata.get("packages")
    if isinstance(packages, dict):
        for manager, package_map in packages.items():
            if not isinstance(manager, str) or not isinstance(package_map, dict):
                continue
            for package_name, version in sorted(package_map.items()):
                if not package_name:
                    continue
                _set_nested_value(
                    roar,
                    ["packages", manager, str(package_name)],
                    version,
                )

    _populate_tracker_labels(roar, metadata)
    _populate_dataset_identifier_labels(roar, metadata.get("dataset_identifiers"))
    _populate_composite_labels(roar, metadata.get("composites"), prefix=["composites"])


def _populate_tracker_labels(roar: dict[str, Any], metadata: dict[str, Any]) -> None:
    analysis = metadata.get("analysis")
    if not isinstance(analysis, dict):
        return
    experiment_tracking = analysis.get("experiment_tracking")
    if not isinstance(experiment_tracking, dict):
        return

    trackers_detected = experiment_tracking.get("trackers_detected")
    trackers = (
        [str(item).strip() for item in trackers_detected if str(item).strip()]
        if isinstance(trackers_detected, list)
        else []
    )
    if trackers:
        _set_nested_value(roar, ["tracker", "count"], len(trackers))
        for tracker in sorted(set(trackers)):
            _set_nested_value(roar, ["tracker", "used", tracker], True)

    raw_runs = experiment_tracking.get("runs")
    runs = (
        [item for item in raw_runs if isinstance(item, dict)] if isinstance(raw_runs, list) else []
    )
    if not runs:
        return

    runs = sorted(runs, key=_tracker_run_sort_key)
    for index, run in enumerate(runs):
        for source_key, label_key in (
            ("tracker", "name"),
            ("url", "url"),
            ("run_id", "run_id"),
            ("project", "project"),
            ("entity", "entity"),
            ("experiment_id", "experiment_id"),
            ("run_name", "run_name"),
            ("status", "status"),
            ("start_time", "start_time"),
            ("tracking_uri", "tracking_uri"),
            ("runtime_seconds", "runtime_seconds"),
        ):
            _set_nested_value(roar, ["tracker", str(index), label_key], run.get(source_key))

    by_name: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        tracker_name = str(run.get("tracker") or "").strip()
        if not tracker_name:
            continue
        by_name.setdefault(tracker_name, []).append(run)

    for tracker_name, tracker_runs in sorted(by_name.items()):
        _set_nested_value(roar, ["tracker", "by_name", tracker_name, "count"], len(tracker_runs))
        if len(tracker_runs) != 1:
            continue
        tracker_run = tracker_runs[0]
        for source_key, label_key in (
            ("url", "url"),
            ("project", "project"),
            ("entity", "entity"),
            ("run_id", "run_id"),
        ):
            _set_nested_value(
                roar,
                ["tracker", "by_name", tracker_name, label_key],
                tracker_run.get(source_key),
            )


def _populate_dataset_identifier_labels(roar: dict[str, Any], raw: Any) -> None:
    identifiers = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if not identifiers:
        return

    identifiers = sorted(
        identifiers,
        key=lambda item: (
            str(item.get("dataset_id") or ""),
            str(item.get("split") or ""),
            str(item.get("version_hint") or ""),
        ),
    )
    _set_nested_value(roar, ["datasets", "count"], len(identifiers))
    for index, identifier in enumerate(identifiers):
        for source_key, label_key in (
            ("dataset_id", "id"),
            ("dataset_fingerprint", "fingerprint"),
            ("dataset_fingerprint_algorithm", "fingerprint_algorithm"),
            ("confidence", "confidence"),
            ("observed_paths", "observed_paths"),
            ("split", "split"),
            ("version_hint", "version_hint"),
        ):
            _set_nested_value(roar, ["datasets", str(index), label_key], identifier.get(source_key))

        evidence = identifier.get("evidence")
        evidence_values = (
            [str(item).strip() for item in evidence if str(item).strip()]
            if isinstance(evidence, list)
            else []
        )
        if evidence_values:
            _set_nested_value(
                roar, ["datasets", str(index), "evidence_count"], len(evidence_values)
            )
            for evidence_index, value in enumerate(sorted(evidence_values)):
                _set_nested_value(
                    roar,
                    ["datasets", str(index), "evidence", str(evidence_index)],
                    value,
                )


def _populate_composite_labels(roar: dict[str, Any], raw: Any, *, prefix: list[str]) -> None:
    composites = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if not composites:
        return

    composites = sorted(
        composites,
        key=lambda item: (str(item.get("root_path") or ""), str(item.get("hash") or "")),
    )
    _set_nested_value(roar, [*prefix, "count"], len(composites))
    for index, composite in enumerate(composites):
        for source_key, label_key in (
            ("hash", "hash"),
            ("root_path", "root_path"),
            ("component_count_total", "component_count_total"),
            ("component_count_stored", "component_count_stored"),
            ("registered", "registered"),
        ):
            _set_nested_value(roar, [*prefix, str(index), label_key], composite.get(source_key))


def _populate_task_labels(
    roar: dict[str, Any], job: Mapping[str, Any], metadata: dict[str, Any]
) -> None:
    kind = str(job.get("job_type") or "").strip().lower()
    if kind not in {"ray_task", "osmo_task"} and not any(
        key in metadata for key in ("task_identity", "task_id", "ray_task_id", "osmo_task_id")
    ):
        return

    _set_nested_value(roar, ["task", "identity"], metadata.get("task_identity"))
    _set_nested_value(
        roar, ["task", "backend"], metadata.get("backend") or job.get("execution_backend")
    )
    _set_nested_value(
        roar,
        ["task", "id"],
        metadata.get("task_id") or metadata.get("ray_task_id") or metadata.get("osmo_task_id"),
    )
    _set_nested_value(roar, ["task", "name"], metadata.get("task_name"))
    _set_nested_value(
        roar,
        ["task", "worker_id"],
        metadata.get("worker_id")
        or metadata.get("ray_worker_id")
        or metadata.get("osmo_worker_id"),
    )
    _set_nested_value(
        roar,
        ["task", "node_id"],
        metadata.get("node_id") or metadata.get("ray_node_id") or metadata.get("osmo_node_id"),
    )
    _set_nested_value(
        roar,
        ["task", "actor_id"],
        metadata.get("actor_id") or metadata.get("ray_actor_id") or metadata.get("osmo_actor_id"),
    )
    _set_nested_value(
        roar,
        ["task", "parent_job_uid"],
        metadata.get("parent_job_uid") or job.get("parent_job_uid"),
    )

    backend_metadata = metadata.get("backend_metadata")
    if isinstance(backend_metadata, dict):
        for source_key, label_key in (
            ("source_job_uid", "job_uid"),
            ("source_execution_backend", "execution_backend"),
            ("source_execution_role", "execution_role"),
            ("source_job_type", "job_type"),
        ):
            _set_nested_value(
                roar,
                ["task", "source", label_key],
                backend_metadata.get(source_key),
            )


def _populate_get_labels(roar: dict[str, Any], metadata: dict[str, Any]) -> None:
    payload = metadata.get("get")
    if not isinstance(payload, dict):
        return
    _set_nested_value(roar, ["get", "source_type"], payload.get("source_type"))
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        _set_nested_value(roar, ["get", "artifact_count"], len(artifacts))
    _set_nested_value(roar, ["git", "commit"], payload.get("git_commit"))
    _set_nested_value(roar, ["git", "tag"], payload.get("git_tag"))


def _populate_put_labels(roar: dict[str, Any], metadata: dict[str, Any]) -> None:
    payload = metadata.get("put")
    if not isinstance(payload, dict):
        return
    _set_nested_value(roar, ["put", "destination_type"], payload.get("destination_type"))
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        _set_nested_value(roar, ["put", "artifact_count"], len(artifacts))
    _set_nested_value(roar, ["git", "commit"], payload.get("git_commit"))
    _set_nested_value(roar, ["git", "tag"], payload.get("git_tag"))
    _populate_dataset_identifier_labels(roar, payload.get("dataset_identifiers"))
    _populate_composite_labels(roar, payload.get("composites"), prefix=["put", "composites"])
    _populate_composite_labels(
        roar,
        payload.get("lineage_composites"),
        prefix=["put", "lineage_composites"],
    )
    _set_nested_value(
        roar,
        ["put", "composites", "registered_count"],
        _count_truthy(payload.get("composites"), key="registered"),
    )
    _set_nested_value(
        roar,
        ["put", "lineage_composites", "registered_count"],
        _count_truthy(payload.get("lineage_composites"), key="registered"),
    )


def _populate_osmo_labels(roar: dict[str, Any], metadata: dict[str, Any], *, kind: str) -> None:
    payload = metadata.get(kind)
    if not isinstance(payload, dict):
        return

    operation = "submit" if kind == "osmo_submit" else "attach"
    _set_nested_value(roar, ["osmo", "operation"], operation)
    _set_nested_value(roar, ["osmo", "workflow_id"], payload.get("workflow_id"))
    _set_nested_value(roar, ["osmo", "workflow_status"], payload.get("workflow_status"))
    _set_nested_value(roar, ["osmo", "wait_for_completion"], payload.get("wait_for_completion"))
    _set_nested_value(
        roar,
        ["osmo", "download_declared_outputs"],
        payload.get("download_declared_outputs"),
    )
    _set_nested_value(
        roar,
        ["osmo", "workflow_query_timed_out"],
        payload.get("workflow_query_timed_out"),
    )
    _set_nested_value(roar, ["git", "commit"], payload.get("git_commit"))

    submit_key = "submit" if kind == "osmo_submit" else "attach"
    submit_payload = payload.get(submit_key)
    if isinstance(submit_payload, dict):
        _set_nested_value(roar, ["osmo", "pool"], submit_payload.get("pool"))
        _set_nested_value(roar, ["osmo", "format_type"], submit_payload.get("format_type"))
        _set_nested_value(
            roar,
            ["osmo", "dataset_hint_count"],
            _safe_list_count(submit_payload.get("dataset_hints")),
        )
        _set_nested_value(
            roar,
            ["osmo", "task_hint_count"],
            _safe_list_count(submit_payload.get("task_name_hints")),
        )
        prepared = submit_payload.get("prepared_workflow")
        if isinstance(prepared, dict):
            _set_nested_value(
                roar,
                ["osmo", "prepared_workflow", "wrapped_task_count"],
                _safe_list_count(prepared.get("wrapped_tasks")),
            )

    downloaded_outputs = payload.get("downloaded_outputs")
    outputs = (
        [item for item in downloaded_outputs if isinstance(item, dict)]
        if isinstance(downloaded_outputs, list)
        else []
    )
    if outputs:
        outputs = sorted(
            outputs,
            key=lambda item: (
                str(item.get("dataset_name") or ""),
                str(item.get("declared_path") or ""),
                str(item.get("task_name") or ""),
            ),
        )
        _set_nested_value(roar, ["osmo", "download_count"], len(outputs))
        _set_nested_value(roar, ["osmo", "downloaded_outputs", "count"], len(outputs))
        for index, output in enumerate(outputs):
            for source_key, label_key in (
                ("dataset_name", "dataset_name"),
                ("declared_path", "declared_path"),
                ("task_name", "task_name"),
                ("file_count", "file_count"),
            ):
                _set_nested_value(
                    roar,
                    ["osmo", "downloaded_outputs", str(index), label_key],
                    output.get(source_key),
                )

    diagnostics = payload.get("workflow_diagnostics")
    if isinstance(diagnostics, dict):
        task_logs = diagnostics.get("task_logs")
        _set_nested_value(
            roar,
            ["osmo", "diagnostics", "task_log_count"],
            _safe_list_count(task_logs),
        )

    lineage = payload.get("lineage_reconstitution")
    if isinstance(lineage, dict):
        for source_key, label_key in (
            ("bundle_count", "bundle_count"),
            ("fragments_processed", "fragments_processed"),
            ("jobs_merged", "jobs_merged"),
            ("artifacts_merged", "artifacts_merged"),
        ):
            _set_nested_value(roar, ["osmo", "lineage", label_key], lineage.get(source_key))

        bundles = lineage.get("bundles")
        bundle_rows = (
            [item for item in bundles if isinstance(item, dict)]
            if isinstance(bundles, list)
            else []
        )
        if bundle_rows:
            bundle_rows = sorted(
                bundle_rows,
                key=lambda item: (
                    str(item.get("dataset_name") or ""),
                    str(item.get("declared_path") or ""),
                    str(item.get("path") or ""),
                ),
            )
            _set_nested_value(roar, ["osmo", "lineage", "bundles", "count"], len(bundle_rows))
            for index, bundle in enumerate(bundle_rows):
                for source_key, label_key in (
                    ("dataset_name", "dataset_name"),
                    ("declared_path", "declared_path"),
                    ("task_name", "task_name"),
                    ("fragments", "fragments"),
                ):
                    _set_nested_value(
                        roar,
                        ["osmo", "lineage", "bundles", str(index), label_key],
                        bundle.get(source_key),
                    )


def _tracker_run_sort_key(run: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(run.get("tracker") or ""),
        str(run.get("url") or ""),
        str(run.get("run_id") or ""),
        str(run.get("project") or ""),
    )


def _safe_list_count(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    return len(value)


def _count_truthy(value: Any, *, key: str) -> int | None:
    if not isinstance(value, list):
        return None
    rows = [item for item in value if isinstance(item, dict)]
    if not rows:
        return 0
    return sum(1 for item in rows if item.get(key) is True)


def _copy_scalar_map(raw: Any, target: dict[str, Any], prefix: list[str]) -> None:
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        _set_nested_value(target, [*prefix, str(key)], value)


def _set_nested_value(target: dict[str, Any], path: list[str], value: Any) -> None:
    if value is None:
        return
    cursor = target
    for key in path[:-1]:
        existing = cursor.get(key)
        if not isinstance(existing, dict):
            existing = {}
            cursor[key] = existing
        cursor = existing
    cursor[path[-1]] = value


def _remove_nested(root: dict[str, Any], path: list[str]) -> None:
    if not path:
        return
    key = path[0]
    if key not in root:
        return
    if len(path) == 1:
        root.pop(key, None)
        return
    child = root.get(key)
    if not isinstance(child, dict):
        return
    _remove_nested(child, path[1:])
    if not child:
        root.pop(key, None)


def _deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
