"""
Click-based CLI for roar.

This module provides the main Click command group and serves as the
entry point for the roar CLI. All 7 commands are implemented as
native Click commands.

Usage:
    from roar.cli import cli
    cli()  # Invokes the CLI
"""

from __future__ import annotations

import click

from .context import RoarContext

# Version is loaded from package metadata
try:
    from importlib.metadata import version

    __version__ = version("roar-cli")
except Exception:
    __version__ = "0.1.11"


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="roar")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """roar - Run Observation & Artifact Registration

    A local front-end to TReqs' Lineage-as-a-Service (GLaaS).
    Tracks data artifacts and execution steps in ML pipelines.

    \b
    Quick Start:
        roar init              Initialize roar in current directory
        roar run <command>     Run a command with provenance tracking

    \b
    Information:
        roar reproduce <hash>  Reproduce an artifact

    \b
    Configuration:
        roar config            View or set configuration
        roar auth              Manage authentication with https://glaas.ai
    """
    ctx.ensure_object(dict)

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)
    else:
        # Create RoarContext for subcommands
        ctx.obj = RoarContext.create()


def register_commands() -> None:
    """Register all CLI commands with the main group.

    This function is called during CLI initialization to register
    all native Click command implementations.
    """
    from .commands import MIGRATED_COMMANDS

    for cmd in MIGRATED_COMMANDS:
        cli.add_command(cmd)


# Register commands at module load time
register_commands()


# Export public API
__all__ = [
    "RoarContext",
    "__version__",
    "cli",
    "register_commands",
]
