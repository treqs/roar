"""
CLI commands for the S3 proxy.

Usage: roar proxy [enable|disable|start|stop|status]
"""

import click

from ...integrations.config import config_get, config_set


@click.group("proxy", invoke_without_command=True)
@click.pass_context
def proxy(ctx: click.Context) -> None:
    """Manage S3 proxy for lineage tracking.

    The proxy intercepts S3 operations during `roar run` and records
    them as inputs (GetObject) and outputs (PutObject) in the lineage.

    \b
    Examples:

        roar proxy                     # Show proxy status
        roar proxy enable              # Enable proxy for roar run
        roar proxy disable             # Disable proxy for roar run
        roar proxy start               # Start standalone daemon
        roar proxy stop                # Stop standalone daemon
    """
    if ctx.invoked_subcommand is None:
        _proxy_status()


def _proxy_status() -> None:
    """Show current proxy configuration and status."""
    from ...execution.cluster.proxy import ProxyService
    from ...integrations.config import get_roar_dir

    enabled = config_get("proxy.enabled") or False
    click.echo(f"Proxy enabled: {enabled}")

    svc = ProxyService()
    binary = svc.find_proxy()
    if binary:
        click.echo(f"Binary: {binary}")
    else:
        click.echo("Binary: not found")

    try:
        roar_dir = get_roar_dir()
        daemon = svc.get_daemon_status(roar_dir)
        if daemon:
            click.echo(f"Daemon: running (pid={daemon['pid']}, port={daemon['port']})")
        else:
            click.echo("Daemon: not running")
    except Exception:
        click.echo("Daemon: not running")


@proxy.command("enable")
def proxy_enable() -> None:
    """Enable the S3 proxy for roar run."""
    try:
        config_path, _ = config_set("proxy.enabled", "true")
        click.echo("S3 proxy enabled for roar run.")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@proxy.command("disable")
def proxy_disable() -> None:
    """Disable the S3 proxy for roar run."""
    try:
        config_path, _ = config_set("proxy.enabled", "false")
        click.echo("S3 proxy disabled.")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@proxy.command("start")
def proxy_start() -> None:
    """Start a standalone S3 proxy daemon.

    The daemon runs in the background. Point S3 clients at its port with
    AWS_ENDPOINT_URL_S3 (SDKs) / S3_ENDPOINT_URL (s5cmd) to route S3 traffic
    through the proxy.
    """
    from ...execution.cluster.proxy import ProxyService
    from ...integrations.config import get_roar_dir

    svc = ProxyService()
    roar_dir = get_roar_dir()

    # Check if already running
    existing = svc.get_daemon_status(roar_dir)
    if existing:
        click.echo(
            f"Proxy daemon already running (pid={existing['pid']}, port={existing['port']})."
        )
        return

    try:
        info = svc.start_daemon(roar_dir)
        click.echo(f"Proxy daemon started (pid={info['pid']}, port={info['port']}).")
        click.echo("")
        click.echo("To use it:")
        click.echo(f"  export AWS_ENDPOINT_URL_S3=http://127.0.0.1:{info['port']}  # boto3, aws cli")
        click.echo(f"  export S3_ENDPOINT_URL=http://127.0.0.1:{info['port']}     # s5cmd")
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e


@proxy.command("stop")
def proxy_stop() -> None:
    """Stop the standalone S3 proxy daemon."""
    from ...execution.cluster.proxy import ProxyService
    from ...integrations.config import get_roar_dir

    svc = ProxyService()
    roar_dir = get_roar_dir()

    if svc.stop_daemon(roar_dir):
        click.echo("Proxy daemon stopped.")
    else:
        click.echo("No proxy daemon was running.")


@proxy.command("status")
def proxy_status_cmd() -> None:
    """Show proxy status."""
    _proxy_status()
