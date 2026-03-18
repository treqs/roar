"""
Native Click implementation of the init command.

Usage: roar init
"""

import sqlite3 as _sqlite3
from pathlib import Path

import click

from roar.db.engine import create_roar_engine, init_database
from roar.db.schema import run_migrations
from roar.execution.framework.registry import iter_execution_backend_init_templates

from ..context import RoarContext

# Default config template with comments
_CORE_CONFIG_TEMPLATE_PREFIX = """\
# roar configuration file
# See: https://docs.roar.dev/configuration

[output]
# Include list of repo files read in provenance output
track_repo_files = false
# Suppress written files report after run
quiet = false

[analyzers]
# Detect experiment trackers (W&B, MLflow, Neptune)
experiment_tracking = true

[filters]
# Ignore system file reads (/sys, /etc, /sbin)
ignore_system_reads = true
# Ignore reads from installed packages (already in dependency list)
ignore_package_reads = true
# Ignore torch/triton cache reads
ignore_torch_cache = true
# Ignore /tmp files entirely
ignore_tmp_files = true

[cleanup]
# Delete /tmp files written during run (strict mode)
delete_tmp_writes = false

[glaas]
# GLaaS server URL
url = "https://api.glaas.ai"
# Path to SSH private key for GLaaS authentication
# key = ""

[registration.omit]
# Enable secret filtering for registration data
enabled = true

[registration.omit.secrets]
# Explicit secret values to always redact
# values = ["my-secret-token"]

[registration.omit.env_vars]
# Environment variable names whose values should be redacted
names = [
    "WANDB_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "DATABASE_URL",
    "AWS_SECRET_ACCESS_KEY",
]

[registration.omit.allowlist]
# Regex patterns that should NOT be redacted (reduce false positives)
# patterns = ["sk-test-"]

# Custom patterns can be added as array of tables:
# [[registration.omit.patterns]]
# id = "slack_webhook"
# pattern = "hooks\\.slack\\.com/services/[A-Z0-9/]+"
# description = "Slack webhook URLs"

# [[registration.omit.patterns]]
# id = "stripe_key"
# pattern = "sk_live_[a-zA-Z0-9]{24,}"
# description = "Stripe live API keys"

# [[registration.omit.patterns]]
# id = "sendgrid_key"
# pattern = "SG\\.[a-zA-Z0-9_-]{22}\\.[a-zA-Z0-9_-]{43}"
# description = "SendGrid API keys"

# [[registration.omit.patterns]]
# id = "twilio_key"
# pattern = "SK[a-f0-9]{32}"
# description = "Twilio API keys"

# [[registration.omit.patterns]]
# id = "mailchimp_key"
# pattern = "[a-f0-9]{32}-us[0-9]{1,2}"
# description = "Mailchimp API keys"

[hash]
# Primary hash algorithm (blake3, sha256, sha512, md5)
primary = "blake3"
# Additional algorithms for roar get
get = ["sha256"]
# Additional algorithms for roar put/upload
put = []
# Additional algorithms for roar run
run = []

[tracer]
# Default tracer backend policy (auto, ebpf, preload, ptrace)
default = "auto"
# Allow runtime fallback to another tracer backend
fallback_enabled = true
"""

_CORE_CONFIG_TEMPLATE_SUFFIX = """\
[reversible]
# Enable file preservation before overwrites during roar run
enabled = false

[logging]
# Log level (debug, info, warning, error)
level = "warning"
# Output debug logs to stderr
console = false
# Output debug logs to ~/.roar/roar.log
file = true

[composites.run]
# Enable local composite materialization during roar run
enabled = true
# Minimum dataset confidence required for auto-materialization
min_confidence = 0.80
# Minimum component leaves required to form a composite
min_components = 2
# Maximum composite roots materialized per run job
max_roots_per_job = 4
"""


def build_default_config_template() -> str:
    sections = [_CORE_CONFIG_TEMPLATE_PREFIX.rstrip()]
    sections.extend(template for template in iter_execution_backend_init_templates())
    sections.append(_CORE_CONFIG_TEMPLATE_SUFFIX.rstrip())
    return "\n\n".join(section for section in sections if section) + "\n"


DEFAULT_CONFIG_TEMPLATE = build_default_config_template()


def _add_to_gitignore(gitignore_path: Path, gitignore_content: str) -> None:
    """Append .roar/ to .gitignore file."""
    with open(gitignore_path, "a") as f:
        if not gitignore_content.endswith("\n"):
            f.write("\n")
        f.write(".roar/\n")


def init_project(cwd: Path) -> Path:
    """Create the minimal local roar project structure in ``cwd``."""
    roar_dir = cwd / ".roar"
    if roar_dir.exists():
        return roar_dir

    roar_dir.mkdir()

    db_path = roar_dir / "roar.db"
    engine = create_roar_engine(db_path)
    init_database(engine)
    engine.dispose()

    raw_conn = _sqlite3.connect(str(db_path))
    raw_conn.row_factory = _sqlite3.Row
    try:
        run_migrations(raw_conn)
        raw_conn.commit()
    finally:
        raw_conn.close()

    config_path = roar_dir / "config.toml"
    config_path.write_text(build_default_config_template())

    return roar_dir


@click.command("init")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Automatically add .roar/ to .gitignore without prompting.",
)
@click.option(
    "--no",
    "-n",
    is_flag=True,
    default=False,
    help="Skip adding .roar/ to .gitignore without prompting.",
)
@click.option(
    "--path",
    "init_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Initialize roar in the given directory instead of the current directory.",
)
@click.pass_obj
def init(ctx: RoarContext, yes: bool, no: bool, init_path: Path | None) -> None:
    """Initialize roar in current directory.

    Creates a .roar directory for storing tracking data, a config.toml
    with default settings, and optionally adds .roar/ to .gitignore.

    \b
    Examples:

        roar init       # Initialize roar, prompt for gitignore

        roar init -y    # Initialize and auto-add to gitignore

        roar init -n    # Initialize without modifying gitignore

        roar init --path /some/dir  # Initialize in a specific directory
    """
    cwd = init_path if init_path is not None else ctx.cwd
    target_repo_root = RoarContext._get_repo_root(cwd)

    # Check if .roar already exists
    roar_dir = cwd / ".roar"
    if roar_dir.exists():
        click.echo(f".roar directory already exists at {roar_dir}")
        return

    init_project(cwd)

    click.echo(f"Created {roar_dir}")
    click.echo(f"Initialized database at {roar_dir / 'roar.db'}")

    # Add privacy/data collection notice
    click.echo("")
    click.echo("roar records file hashes, commands, and dependency metadata.")
    click.echo("It does not upload file contents to GLaaS.")
    click.echo("")

    click.echo(f"Created {roar_dir / 'config.toml'}")

    # Check if we're in a git repo
    if target_repo_root is None:
        click.echo("Not in a git repository. Done.")
        return

    # Check if .gitignore exists
    gitignore_path = target_repo_root / ".gitignore"
    if not gitignore_path.exists():
        click.echo("No .gitignore found. Done.")
        return

    # Check if .roar is already in .gitignore
    gitignore_content = gitignore_path.read_text()
    if ".roar" in gitignore_content or ".roar/" in gitignore_content:
        click.echo(".roar is already in .gitignore. Done.")
        return

    # Handle gitignore update
    click.echo("")
    if yes:
        # Auto-confirm with --yes flag
        _add_to_gitignore(gitignore_path, gitignore_content)
        click.echo("Added .roar/ to .gitignore")
    elif no:
        # Skip with --no flag
        click.echo("Skipped .gitignore update.")
    else:
        import sys as _sys

        if not _sys.stdin.isatty():
            # Non-interactive: skip to avoid blocking
            click.echo("Skipped .gitignore update (non-interactive).")
        elif click.confirm("Add .roar/ to .gitignore?", default=True):
            _add_to_gitignore(gitignore_path, gitignore_content)
            click.echo("Added .roar/ to .gitignore")
        else:
            click.echo("Skipped .gitignore update.")

    click.echo("Done.")
