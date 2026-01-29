"""
Native Click implementation of the init command.

Usage: roar init
"""

from pathlib import Path

import click

from ..context import RoarContext

# Default config template with comments
DEFAULT_CONFIG_TEMPLATE = """\
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
"""


def _add_to_gitignore(gitignore_path: Path, gitignore_content: str) -> None:
    """Append .roar/ to .gitignore file."""
    with open(gitignore_path, "a") as f:
        if not gitignore_content.endswith("\n"):
            f.write("\n")
        f.write(".roar/\n")


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
@click.pass_obj
def init(ctx: RoarContext, yes: bool, no: bool) -> None:
    """Initialize roar in current directory.

    Creates a .roar directory for storing tracking data, a config.toml
    with default settings, and optionally adds .roar/ to .gitignore.

    \b
    Examples:

        roar init       # Initialize roar, prompt for gitignore

        roar init -y    # Initialize and auto-add to gitignore

        roar init -n    # Initialize without modifying gitignore
    """
    cwd = ctx.cwd

    # Check if .roar already exists
    roar_dir = cwd / ".roar"
    if roar_dir.exists():
        click.echo(f".roar directory already exists at {roar_dir}")
        return

    # Create .roar directory
    roar_dir.mkdir()
    click.echo(f"Created {roar_dir}")

    # Add privacy/data collection notice
    click.echo("")
    click.echo("roar records file hashes, commands, and dependency metadata.")
    click.echo("It does not upload file contents to GLaaS.")
    click.echo("")

    # Create default config.toml
    config_path = roar_dir / "config.toml"
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE)
    click.echo(f"Created {config_path}")

    # Check if we're in a git repo
    if ctx.repo_root is None:
        click.echo("Not in a git repository. Done.")
        return

    # Check if .gitignore exists
    gitignore_path = ctx.repo_root / ".gitignore"
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
    elif click.confirm("Add .roar/ to .gitignore?", default=True):
        _add_to_gitignore(gitignore_path, gitignore_content)
        click.echo("Added .roar/ to .gitignore")
    else:
        click.echo("Skipped .gitignore update.")

    click.echo("Done.")
