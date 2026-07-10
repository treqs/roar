"""Bundle-mode fragment fallback for GLaaS-less pods.

When pods cannot reach GLaaS, the pod entrypoint writes its execution
fragment as ``roar-fragments-<pod>.json`` into ``ROAR_K8S_BUNDLE_DIR``
(a mounted shared volume declared via ``k8s.bundle_dir``). Someone with
access to that volume later runs ``roar k8s ingest-bundles <dir>`` to
merge the bundles into the local lineage DB — the OSMO bundle pattern,
k8s-shaped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roar.backends.k8s.lineage import collect_k8s_fragments, resolve_active_session_context

BUNDLE_FILENAME_PREFIX = "roar-fragments-"


class K8sBundleError(RuntimeError):
    """Raised when bundle ingestion cannot proceed, with actionable detail."""


@dataclass(frozen=True)
class K8sBundleIngestResult:
    bundles_ingested: int
    fragments_merged: int
    bundle_paths: list[str]


def bundle_filename_for_pod(pod_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", pod_name.strip() or "pod")
    return f"{BUNDLE_FILENAME_PREFIX}{safe}.json"


def write_fragment_bundle(bundle_dir: Path, pod_name: str, fragments: list[dict[str, Any]]) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    target = bundle_dir / bundle_filename_for_pod(pod_name)
    target.write_text(
        json.dumps({"fragments": fragments}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def discover_fragment_bundles(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{BUNDLE_FILENAME_PREFIX}*.json"))


def ingest_fragment_bundles(
    *,
    roar_dir: Path,
    directory: Path,
) -> K8sBundleIngestResult:
    bundles = discover_fragment_bundles(directory)
    if not bundles:
        raise K8sBundleError(
            f"no {BUNDLE_FILENAME_PREFIX}*.json bundles found in {directory}; "
            "point at the directory pods wrote via k8s.bundle_dir"
        )

    fragments: list[dict[str, Any]] = []
    for bundle_path in bundles:
        try:
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise K8sBundleError(f"cannot read bundle {bundle_path}: {exc}") from exc
        items = payload.get("fragments") if isinstance(payload, dict) else None
        fragments.extend(item for item in items or [] if isinstance(item, dict))

    if not fragments:
        raise K8sBundleError(f"bundles in {directory} contain no fragments")

    db_path = roar_dir / "roar.db"
    session_id, step_number = resolve_active_session_context(str(db_path))
    driver_job_uid = next(
        (
            str(fragment.get("parent_job_uid") or "").strip()
            for fragment in fragments
            if str(fragment.get("parent_job_uid") or "").strip()
        ),
        None,
    )
    merged = collect_k8s_fragments(
        fragments,
        project_dir=str(roar_dir.parent),
        driver_job_uid=driver_job_uid,
        session_id=session_id,
        step_number=step_number,
    )
    return K8sBundleIngestResult(
        bundles_ingested=len(bundles),
        fragments_merged=merged,
        bundle_paths=[str(path) for path in bundles],
    )


__all__ = [
    "BUNDLE_FILENAME_PREFIX",
    "K8sBundleError",
    "K8sBundleIngestResult",
    "bundle_filename_for_pod",
    "discover_fragment_bundles",
    "ingest_fragment_bundles",
    "write_fragment_bundle",
]
