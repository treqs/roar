from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from roar.execution.fragments.export import export_local_job_fragment_bundle


@dataclass(frozen=True)
class OsmoLineageBundleExport:
    output_path: str
    exported_job_uid: str
    fragment_count: int
    task_id: str
    task_name: str


def export_osmo_lineage_bundle(
    *,
    roar_dir: Path,
    output_path: Path,
    job_uid: str | None = None,
    task_id: str | None = None,
    task_name: str | None = None,
    backend_name: str = "osmo",
) -> OsmoLineageBundleExport:
    export = export_local_job_fragment_bundle(
        roar_dir=roar_dir,
        output_path=output_path,
        backend_name=str(backend_name or "osmo").strip() or "osmo",
        job_uid=job_uid,
        task_id=task_id,
        task_name=task_name,
        default_task_name="osmo-task",
    )
    return OsmoLineageBundleExport(
        output_path=export.output_path,
        exported_job_uid=export.exported_job_uid,
        fragment_count=export.fragment_count,
        task_id=export.task_id,
        task_name=export.task_name,
    )


__all__ = [
    "OsmoLineageBundleExport",
    "export_osmo_lineage_bundle",
]
