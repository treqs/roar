"""Native Click implementation of the telemetry command."""

from __future__ import annotations

from pathlib import Path

import click

from ... import telemetry as telemetry_core
from ..context import RoarContext


@click.command("telemetry")
@click.option("--status", "show_status", is_flag=True, help="Show telemetry status.")
@click.option("--print", "print_payload", is_flag=True, help="Print the next payload preview.")
@click.option("--disable", "disable", is_flag=True, help="Disable telemetry globally.")
@click.option("--enable", "enable", is_flag=True, help="Enable telemetry globally.")
@click.option("--endpoint", "endpoint", default=None, help="Set the global telemetry endpoint.")
@click.pass_obj
def telemetry(
    ctx: RoarContext,
    show_status: bool,
    print_payload: bool,
    disable: bool,
    enable: bool,
    endpoint: str | None,
) -> None:
    """Inspect and configure anonymous product telemetry."""
    start_dir = ctx.cwd if ctx is not None else Path.cwd()
    selected = sum(bool(value) for value in (show_status, print_payload, disable, enable))
    selected += endpoint is not None
    if selected == 0:
        show_status = True
    elif selected > 1:
        raise click.ClickException("Choose only one telemetry action.")

    if disable:
        path = telemetry_core.set_global_enabled(False)
        click.echo(f"Telemetry disabled globally in {path}")
        return

    if enable:
        path = telemetry_core.set_global_enabled(True)
        click.echo(f"Telemetry enabled globally in {path}")
        return

    if endpoint is not None:
        path = telemetry_core.set_global_endpoint(endpoint)
        click.echo(f"Telemetry endpoint saved in {path}")
        return

    if print_payload:
        snapshot = telemetry_core.status_snapshot(start_dir=start_dir)
        if not snapshot["uploads_enabled"]:
            raise click.ClickException(_print_disabled_message(snapshot))
        click.echo(telemetry_core.print_payload(start_dir=start_dir))
        return

    if show_status:
        _print_status(telemetry_core.status_snapshot(start_dir=start_dir))


def _print_status(snapshot: dict[str, object]) -> None:
    state = "enabled" if snapshot["enabled"] else "disabled"
    uploads = "enabled" if snapshot["uploads_enabled"] else "disabled"
    click.echo(f"Telemetry: {state}")
    click.echo(f"Uploads: {uploads}")
    click.echo(f"Endpoint: {snapshot['endpoint']} ({snapshot['endpoint_source']})")
    click.echo(f"Queued events: {snapshot['queued_event_count']}")
    click.echo(f"Install ID: {snapshot['install_id'] or '(not created)'}")
    if snapshot.get("disabled_reason"):
        click.echo(f"Disabled reason: {snapshot['disabled_reason']}")
    if snapshot.get("suppression_reason"):
        click.echo(f"Suppression: {snapshot['suppression_reason']}")
    click.echo(f"Global config: {snapshot['global_config_file']}")
    if snapshot.get("project_config_file"):
        click.echo(f"Project config: {snapshot['project_config_file']}")


def _print_disabled_message(snapshot: dict[str, object]) -> str:
    reason = (
        snapshot.get("suppression_reason")
        or snapshot.get("disabled_reason")
        or ("empty_endpoint" if not snapshot.get("endpoint") else "uploads_disabled")
    )
    return f"No telemetry payload would be uploaded: {reason}."
