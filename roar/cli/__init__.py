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

import os
from collections.abc import Iterable
from importlib import import_module

import click

from .command_registry import COMMAND_SPECS, build_help_groups, build_lazy_commands

# Version is loaded from package metadata
try:
    from importlib.metadata import version

    __version__ = version("roar-cli")
except Exception:
    __version__ = "0.2.10"


# Lazy command registry: maps command name to (module_path, command_name, short_help)
# Short help is stored here to avoid importing commands just for --help
LAZY_COMMANDS: dict[str, tuple[str, str, str]] = build_lazy_commands()

HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = build_help_groups()

EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG = "ROAR_ENABLE_EXPERIMENTAL_ACCOUNT_COMMANDS"
_EXPERIMENTAL_ACCOUNT_COMMANDS = frozenset(
    {
        "login",
        "logout",
        "whoami",
        "projects",
        "workflow",
    }
)
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes"})


def experimental_account_commands_enabled() -> bool:
    """Return whether unreleased account commands should be exposed."""
    return (
        os.environ.get(EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG, "").strip().lower() in _TRUTHY_ENV_VALUES
    )


def _command_is_available(command_name: str) -> bool:
    if command_name not in _EXPERIMENTAL_ACCOUNT_COMMANDS:
        return True
    return experimental_account_commands_enabled()


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
            try:
                module = import_module(self._module_path)
            except ModuleNotFoundError as exc:
                missing = exc.name or "unknown"
                raise click.ClickException(
                    f"Failed to load '{self.name}' because import '{missing}' is unavailable. "
                    "Reinstall roar-cli or run it from a fully provisioned environment."
                ) from exc
            except ImportError as exc:
                raise click.ClickException(f"Failed to load '{self.name}': {exc}") from exc
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
        return sorted(name for name in super().list_commands(ctx) if _command_is_available(name))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve commands, hiding unreleased account commands by default."""
        if not _command_is_available(cmd_name):
            return None
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Render top-level commands grouped by workflow."""
        rendered_commands: set[str] = set()

        for section_name, command_names in HELP_GROUPS:
            rows = self._get_command_rows(ctx, formatter, command_names)
            if not rows:
                continue
            rendered_commands.update(name for name, _ in rows)
            with formatter.section(section_name):
                formatter.write_dl(rows)

        remaining = [name for name in self.list_commands(ctx) if name not in rendered_commands]
        rows = self._get_command_rows(ctx, formatter, remaining)
        if rows:
            with formatter.section("Other Commands"):
                formatter.write_dl(rows)

    def _get_command_rows(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
        command_names: Iterable[str],
    ) -> list[tuple[str, str]]:
        """Build help rows for a command section."""
        commands: list[tuple[str, click.Command]] = []
        max_name_len = 0

        for command_name in command_names:
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            commands.append((command_name, command))
            max_name_len = max(max_name_len, len(command_name))

        if not commands:
            return []

        help_limit = formatter.width - 6 - max_name_len
        return [
            (command_name, command.get_short_help_str(help_limit))
            for command_name, command in commands
        ]


@click.command(cls=LazyGroup, lazy_commands=LAZY_COMMANDS, invoke_without_command=True)
@click.version_option(version=__version__, prog_name="roar")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """roar - Run Observation & Artifact Registration

    A local front-end to GLaaS (Global Lineage as a Service).
    Tracks data artifacts and execution steps in ML pipelines.

    \b
    Quick Start:
        roar init              Initialize roar in current directory
        roar run <command>     Run a command with provenance tracking

    \b
    Inspect Local Lineage:
        roar status            Show the active session summary
        roar show @1           Inspect a specific step, job, or artifact
        roar dag               See the full local execution graph

    \b
    Share and Publish:
        roar put ...           Upload artifacts and register lineage
        roar register <target> Register local lineage with GLaaS

    \b
    Setup and Configuration:
        roar config            View or set configuration
        roar auth key          Show the SSH key used for GLaaS signup
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
    "COMMAND_SPECS",
    "EXPERIMENTAL_ACCOUNT_COMMANDS_FLAG",
    "LazyGroup",
    "__version__",
    "cli",
    "experimental_account_commands_enabled",
]
