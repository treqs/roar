"""
Native Click implementation of the config command.

Usage: roar config [list|get|set] [key] [value]
"""

import click

from ...integrations.config import ConfigSetResult, config_get, config_list, config_set


def _echo_config_set_warnings(result: ConfigSetResult) -> None:
    if not result.warnings:
        return

    click.echo("Warning: other config fields are invalid:")
    for warning in result.warnings:
        click.echo(f"  - {warning}")


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
        result = config_set(key, value)
        config_path, typed_value = result
        click.echo(f"Set {key} = {typed_value}")
        click.echo(f"Saved to {config_path}")
        _echo_config_set_warnings(result)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
