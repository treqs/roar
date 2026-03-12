from __future__ import annotations

from typing import TYPE_CHECKING

import click

from roar.glaas_client import get_glaas_url
from roar.services.execution.distributed_backends import get_execution_backend
from roar.services.execution.fragment_sessions import load_fragment_session

if TYPE_CHECKING:
    from roar.cli.context import RoarContext
    from roar.services.execution.distributed_backends import SubmitRunFinalizer


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

    adapter = backend.fragment_reconstitution
    if adapter is None:
        return

    try:
        session_payload = load_fragment_session(ctx.roar_dir, session_id)
        token = session_payload.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("missing token in fragment session payload")
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
        ).reconstitute()
    except Exception as exc:
        click.echo(
            f"[roar] warning: lineage reconstitution failed for session {session_id}: {exc}",
            err=True,
        )
        return

    click.echo(
        f"[roar] lineage reconstituted: {result.jobs_merged} jobs, {result.artifacts_merged} artifacts"
    )
