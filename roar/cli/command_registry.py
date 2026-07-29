"""Static command-set registry for the top-level roar CLI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """Lazy-load metadata for a top-level CLI command."""

    name: str
    module_path: str
    attr_name: str
    short_help: str
    help_section: str | None = None
    hidden: bool = False


_HELP_SECTION_ORDER: tuple[str, ...] = (
    "Start Here",
    "Sessions",
    "Inspect Local Lineage",
    "Share and Publish",
    "Setup and Admin",
    "GLaaS / TReqs Account",
)

_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("init", "roar.cli.commands.init", "init", "Set up roar in a project", "Start Here"),
    CommandSpec(
        "run", "roar.cli.commands.run", "run", "Track a command with provenance", "Start Here"
    ),
    CommandSpec(
        "build",
        "roar.cli.commands.build",
        "build",
        "Track a build step before the main pipeline",
        "Start Here",
    ),
    CommandSpec(
        "status",
        "roar.cli.commands.status",
        "status",
        "Show the active session summary",
        "Sessions",
    ),
    CommandSpec(
        "session",
        "roar.cli.commands.session",
        "session",
        "Inspect the active session (e.g. its hash)",
        "Sessions",
    ),
    CommandSpec(
        "dag",
        "roar.cli.commands.dag",
        "dag",
        "Inspect the local execution DAG",
        "Sessions",
    ),
    CommandSpec(
        "pop",
        "roar.cli.commands.pop",
        "pop",
        "Undo the last job in the active session",
        "Sessions",
    ),
    CommandSpec(
        "reset",
        "roar.cli.commands.reset",
        "reset",
        "Start a fresh session (boundary between pipelines)",
        "Sessions",
    ),
    CommandSpec(
        "log",
        "roar.cli.commands.log",
        "log",
        "List jobs in the active session",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "show",
        "roar.cli.commands.show",
        "show",
        "Inspect a session, job, or artifact",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "diff",
        "roar.cli.commands.diff",
        "diff",
        "Compare provenance of two artifacts or steps",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "tui",
        "roar.cli.commands.tui",
        "tui",
        "Launch the interactive terminal UI",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "lineage",
        "roar.cli.commands.lineage",
        "lineage",
        "Inspect lineage for a tracked artifact",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "inputs",
        "roar.cli.commands.inputs",
        "inputs",
        "Show root input artifacts for a target",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "reproduce",
        "roar.cli.commands.reproduce",
        "reproduce",
        "Generate a reproduction plan",
        "Inspect Local Lineage",
    ),
    CommandSpec(
        "put",
        "roar.cli.commands.put",
        "put",
        "Publish artifacts and register lineage",
        "Share and Publish",
    ),
    CommandSpec(
        "register",
        "roar.cli.commands.register",
        "register",
        "Register local lineage with GLaaS",
        "Share and Publish",
    ),
    CommandSpec(
        "export-registration-package",
        "roar.cli.commands.export_registration_package",
        "export_registration_package",
        "Export local lineage for server-side registration",
        "Share and Publish",
        hidden=True,
    ),
    CommandSpec(
        "get", "roar.cli.commands.get", "get", "Download published artifacts", "Share and Publish"
    ),
    CommandSpec(
        "label", "roar.cli.commands.label", "label", "Manage local labels", "Share and Publish"
    ),
    CommandSpec(
        "tag",
        "roar.cli.commands.tag",
        "tag",
        "Manage hereditary compliance tags",
        "Share and Publish",
    ),
    CommandSpec(
        "auth",
        "roar.cli.commands.auth",
        "auth",
        "Manage GLaaS auth and SSH keys",
        "GLaaS / TReqs Account",
    ),
    CommandSpec(
        "config",
        "roar.cli.commands.config",
        "config",
        "View or set configuration",
        "Setup and Admin",
    ),
    CommandSpec(
        "db",
        "roar.cli.commands.db",
        "db",
        "Inspect the local database (size, contents, sync, hygiene)",
        "Setup and Admin",
    ),
    CommandSpec(
        "filter",
        "roar.cli.commands.filter",
        "filter_group",
        "Manage project-level path filters (.roarconfig)",
        "Setup and Admin",
    ),
    CommandSpec(
        "env",
        "roar.cli.commands.env",
        "env",
        "Manage persistent environment variables",
        "Setup and Admin",
    ),
    CommandSpec(
        "tracer",
        "roar.cli.commands.tracer",
        "tracer",
        "Configure tracer backend defaults",
        "Setup and Admin",
    ),
    CommandSpec(
        "telemetry",
        "roar.cli.commands.telemetry",
        "telemetry",
        "Inspect or disable anonymous product telemetry",
        "Setup and Admin",
    ),
    CommandSpec(
        "proxy",
        "roar.cli.commands.proxy",
        "proxy",
        "Manage S3 proxy for lineage tracking",
        "Setup and Admin",
    ),
    CommandSpec(
        "login",
        "roar.cli.commands.login",
        "login",
        "Store global GLaaS/TReqs auth state",
        "GLaaS / TReqs Account",
    ),
    CommandSpec(
        "logout",
        "roar.cli.commands.logout",
        "logout",
        "Clear global GLaaS/TReqs auth state",
        "GLaaS / TReqs Account",
    ),
    CommandSpec(
        "whoami",
        "roar.cli.commands.whoami",
        "whoami",
        "Show current GLaaS/TReqs login and repo binding",
        "GLaaS / TReqs Account",
    ),
    CommandSpec(
        "projects",
        "roar.cli.commands.projects",
        "projects",
        "Manage GLaaS projects visible through your TReqs account",
        "GLaaS / TReqs Account",
        hidden=True,
    ),
    CommandSpec(
        "scope",
        "roar.cli.commands.scope",
        "scope",
        "Show or change where this repo publishes lineage",
        "GLaaS / TReqs Account",
    ),
    CommandSpec(
        "workflow",
        "roar.cli.commands.workflow",
        "workflow",
        "Generate TReqs workflow YAML from local sessions",
        "GLaaS / TReqs Account",
    ),
    CommandSpec("osmo", "roar.cli.commands.osmo", "osmo", "Manage OSMO workflow attachment"),
    CommandSpec(
        "k8s", "roar.cli.commands.k8s", "k8s", "Prepare Kubernetes Jobs for lineage capture"
    ),
)


def iter_command_specs() -> tuple[CommandSpec, ...]:
    """Return the registered top-level command specs."""
    return _COMMAND_SPECS


def build_lazy_commands() -> dict[str, tuple[str, str, str, bool]]:
    """Build the lazy-command registry consumed by ``LazyGroup``."""
    return {
        spec.name: (spec.module_path, spec.attr_name, spec.short_help, spec.hidden)
        for spec in iter_command_specs()
    }


def build_help_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Build grouped help sections from registered command specs."""
    sections: dict[str, list[str]] = {section: [] for section in _HELP_SECTION_ORDER}
    for spec in iter_command_specs():
        if spec.hidden or spec.help_section is None:
            continue
        sections.setdefault(spec.help_section, []).append(spec.name)

    return tuple(
        (section, tuple(sections.get(section, ())))
        for section in _HELP_SECTION_ORDER
        if sections.get(section)
    )


COMMAND_SPECS = iter_command_specs()

__all__ = [
    "COMMAND_SPECS",
    "CommandSpec",
    "build_help_groups",
    "build_lazy_commands",
    "iter_command_specs",
]
