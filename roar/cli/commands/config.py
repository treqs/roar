"""
Native Click implementation of the config command.

Usage: roar config [list|get|set] [key] [value]
"""

import click

from ...config import config_get, config_list, config_set


@click.group("config", invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """View or set configuration.

    Config is stored in .roar/config.toml

    \b
    Examples:

        roar config list                     # List all options

        roar config get registration.omit.enabled  # Get a value

        roar config set output.quiet true    # Set a value
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config.command("list")
def config_list_cmd() -> None:
    """List all config options."""
    keys = config_list()
    click.echo("Available config options:")
    click.echo("")

    for key, info in keys.items():
        default = info["default"]
        desc = info["description"]
        click.echo(f"  {key}")
        click.echo(f"    {desc}")
        click.echo(f"    Default: {default}")
        click.echo("")


@config.command("get")
@click.argument("key")
def config_get_cmd(key: str) -> None:
    """Get a config value.

    Arguments:

        KEY    The config key to get (e.g. registration.omit.enabled)
    """
    value = config_get(key)
    if value is None:
        click.echo(f"{key}: (not set)")
    else:
        click.echo(f"{key}: {value}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set_cmd(key: str, value: str) -> None:
    """Set a config value.

    Arguments:

        KEY    The config key to set

        VALUE  The value to set
    """
    try:
        config_path, typed_value = config_set(key, value)
        click.echo(f"Set {key} = {typed_value}")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e
