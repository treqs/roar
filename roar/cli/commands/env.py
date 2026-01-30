"""
Native Click implementation of the env command.

Usage: roar env [set|get|list|unset] [NAME] [VALUE]
"""

import click

from ...config import get_config_path_for_write, load_config, save_config


def _load_env_vars() -> dict[str, str]:
    """Load env vars from config."""
    config = load_config()
    return dict(config.get("env", {}))


def _save_env_vars(env_vars: dict[str, str]) -> None:
    """Save env vars to config."""
    config = load_config()
    config["env"] = env_vars
    config_path = get_config_path_for_write()
    save_config(config, config_path)


@click.group("env", invoke_without_command=True)
@click.pass_context
def env(ctx: click.Context) -> None:
    """Manage persistent environment variables.

    Environment variables are stored in .roar/config.toml and injected
    into subprocess environments during roar build and roar run.

    \b
    Examples:

        roar env set FOO bar        # Set FOO=bar
        roar env get FOO            # Print value of FOO
        roar env list               # List all env vars
        roar env unset FOO          # Remove FOO
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@env.command("set")
@click.argument("name")
@click.argument("value")
def env_set(name: str, value: str) -> None:
    """Set an environment variable.

    Arguments:

        NAME   The environment variable name
        VALUE  The value to set
    """
    env_vars = _load_env_vars()
    env_vars[name] = value
    _save_env_vars(env_vars)
    click.echo(f"Set {name}={value}")


@env.command("get")
@click.argument("name")
def env_get(name: str) -> None:
    """Get an environment variable value.

    Arguments:

        NAME   The environment variable name
    """
    env_vars = _load_env_vars()
    if name in env_vars:
        click.echo(env_vars[name])
    else:
        raise click.ClickException(f"Environment variable not set: {name}")


@env.command("list")
def env_list() -> None:
    """List all environment variables."""
    env_vars = _load_env_vars()
    if not env_vars:
        click.echo("No environment variables set.")
        return
    for name, value in sorted(env_vars.items()):
        click.echo(f"{name}={value}")


@env.command("unset")
@click.argument("name")
def env_unset(name: str) -> None:
    """Remove an environment variable.

    Arguments:

        NAME   The environment variable name to remove
    """
    env_vars = _load_env_vars()
    if name not in env_vars:
        raise click.ClickException(f"Environment variable not set: {name}")
    del env_vars[name]
    _save_env_vars(env_vars)
    click.echo(f"Unset {name}")
