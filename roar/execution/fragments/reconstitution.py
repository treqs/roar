from __future__ import annotations

from typing import TYPE_CHECKING

import click

from roar.execution.fragments.sessions import load_fragment_session
from roar.execution.framework.registry import get_execution_backend
from roar.integrations.glaas import get_glaas_url

if TYPE_CHECKING:
    from roar.cli.context import RoarContext
    from roar.execution.framework.contract import SubmitRunFinalizer


def build_submit_finalizer(backend_name: str, session_id: str) -> SubmitRunFinalizer:
    def _finalize(ctx: RoarContext) -> None:
        _maybe_reconstitute_lineage(ctx, backend_name=backend_name, session_id=session_id)

    return _finalize


def _maybe_reconstitute_lineage(ctx: RoarContext, *, backend_name: str, session_id: str) -> None:
    glaas_url = get_glaas_url()
    if not glaas_url:
        return

    try:
        backend = get_execution_backend(backend_name)
    except Exception as exc:
        click.echo(
            f"[roar] warning: unknown execution backend {backend_name!r} for session "
            f"{session_id}: {exc}",
            err=True,
        )
        return

    distributed = backend.distributed
    adapter = distributed.fragment_reconstitution if distributed is not None else None
    if adapter is None:
        return

    try:
        session_payload = load_fragment_session(ctx.roar_dir, session_id)
        token = session_payload.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("missing token in fragment session payload")
        driver_job_uid_value = session_payload.get("driver_job_uid")
        driver_job_uid = (
            driver_job_uid_value.strip()
            if isinstance(driver_job_uid_value, str) and driver_job_uid_value.strip()
            else None
        )
    except Exception as exc:
        click.echo(
            f"[roar] warning: failed to load fragment session for session {session_id}: {exc}",
            err=True,
        )
        return

    try:
        result = adapter.create_reconstituter(
            session_id,
            token,
            str(glaas_url),
            ctx.roar_dir / "roar.db",
        ).reconstitute(driver_job_uid=driver_job_uid)
    except Exception as exc:
        click.echo(
            f"[roar] warning: lineage reconstitution failed for session {session_id}: {exc}",
            err=True,
        )
        return

    click.echo(
        f"[roar] lineage reconstituted: {result.jobs_merged} jobs, {result.artifacts_merged} artifacts"
    )
    batches_fetched = int(getattr(result, "batches_fetched", 0) or 0)
    fragments_decrypted = int(getattr(result, "fragments_decrypted", 0) or 0)
    fragments_processed = int(getattr(result, "fragments_processed", 0) or 0)
    fetch_attempts = int(getattr(result, "fetch_attempts", 0) or 0)
    click.echo(
        "[roar] lineage fragments: "
        f"{batches_fetched} batches, {fragments_decrypted} decrypted, "
        f"{fragments_processed} processed, "
        f"{fetch_attempts} fetch attempts"
    )
    error = str(getattr(result, "error", "") or "").strip()
    if error:
        click.echo(
            f"[roar] warning: lineage reconstitution incomplete for session {session_id}: {error}",
            err=True,
        )
