"""
Click-based CLI for roar.

This module provides the main Click command group and serves as the
entry point for the roar CLI. All commands are implemented as
native Click commands with lazy loading for fast startup.

Usage:
    from roar.cli import cli
    cli()  # Invokes the CLI
"""

from __future__ import annotations

from importlib import import_module

import click

# Version is loaded from package metadata
try:
    from importlib.metadata import version

    __version__ = version("roar-cli")
except Exception:
    __version__ = "0.1.11"


# Lazy command registry: maps command name to (module_path, command_name, short_help)
# Short help is stored here to avoid importing commands just for --help
LAZY_COMMANDS: dict[str, tuple[str, str, str]] = {
    "auth": ("roar.cli.commands.auth", "auth", "Manage authentication with GLaaS"),
    "build": ("roar.cli.commands.build", "build", "Run a build step before the main pipeline"),
    "config": ("roar.cli.commands.config", "config", "View or set configuration"),
    "dag": ("roar.cli.commands.dag", "dag", "Show the execution DAG"),
    "env": ("roar.cli.commands.env", "env", "Show environment information"),
    "get": ("roar.cli.commands.get", "get", "Download artifacts from cloud storage"),
    "init": ("roar.cli.commands.init", "init", "Initialize roar in current directory"),
    "lineage": ("roar.cli.commands.lineage", "lineage", "Show lineage for an artifact"),
    "log": ("roar.cli.commands.log", "log", "Show execution log"),
    "pop": ("roar.cli.commands.pop", "pop", "Pop the last step from the session"),
    "put": ("roar.cli.commands.put", "put", "Publish artifacts to cloud storage"),
    "register": ("roar.cli.commands.register", "register", "Register artifacts or jobs"),
    "reproduce": ("roar.cli.commands.reproduce", "reproduce", "Reproduce an artifact"),
    "reset": ("roar.cli.commands.reset", "reset", "Reset roar state"),
    "run": ("roar.cli.commands.run", "run", "Run a command with provenance tracking"),
    "show": ("roar.cli.commands.show", "show", "Show details of a job or artifact"),
    "status": ("roar.cli.commands.status", "status", "Show current session status"),
}


class LazyCommand(click.Command):
    """A placeholder command that loads the real implementation on invoke."""

    def __init__(self, name: str, module_path: str, attr_name: str, short_help: str):
        super().__init__(name=name, callback=None, help=short_help)
        self._module_path = module_path
        self._attr_name = attr_name
        self._real_command: click.Command | None = None

    def _load(self) -> click.Command:
        """Load the real command implementation."""
        if self._real_command is None:
            module = import_module(self._module_path)
            self._real_command = getattr(module, self._attr_name)
        return self._real_command

    def invoke(self, ctx: click.Context) -> None:
        """Load and invoke the real command."""
        real_cmd = self._load()
        return real_cmd.invoke(ctx)

    def get_help(self, ctx: click.Context) -> str:
        """Load the real command to get full help."""
        real_cmd = self._load()
        return real_cmd.get_help(ctx)

    def get_params(self, ctx: click.Context) -> list:
        """Load the real command to get params."""
        real_cmd = self._load()
        return real_cmd.get_params(ctx)

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra,
    ) -> click.Context:
        """Load the real command to make context."""
        real_cmd = self._load()
        return real_cmd.make_context(info_name, args, parent=parent, **extra)


class LazyGroup(click.Group):
    """A Click group that lazily loads commands on first use.

    This dramatically improves CLI startup time by only importing
    command modules when they are actually invoked.
    """

    def __init__(
        self, *args, lazy_commands: dict[str, tuple[str, str, str]] | None = None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self._lazy_commands = lazy_commands or {}
        # Create placeholder commands for help rendering
        for name, (module_path, attr_name, short_help) in self._lazy_commands.items():
            self.add_command(LazyCommand(name, module_path, attr_name, short_help), name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        """List all available commands."""
        return sorted(super().list_commands(ctx))


@click.command(cls=LazyGroup, lazy_commands=LAZY_COMMANDS, invoke_without_command=True)
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
        # Create RoarContext for subcommands (lazy import)
        from .context import RoarContext

        ctx.obj = RoarContext.create()


# Export public API
__all__ = [
    "LazyGroup",
    "__version__",
    "cli",
]
