"""
Native Click implementation of the init command.

Usage: roar init
"""

import sqlite3 as _sqlite3
from pathlib import Path

import click

from roar.db.context import create_database_context
from roar.db.engine import create_roar_engine, init_database
from roar.db.schema import run_migrations
from roar.execution.framework.registry import iter_execution_backend_init_templates

from ..context import RoarContext

# Default config template with comments
_CORE_CONFIG_TEMPLATE_PREFIX = """\
# roar configuration file

[output]
# Include list of repo files read in provenance output
track_repo_files = false
# Output level: "quiet" (silent), "normal" (status quo + filter counts),
# "verbose" (also list read/written files), "debug" (also list filtered files).
verbosity = "normal"

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
# Ignore well-known per-library user-cache subdirs (~/.cache/huggingface,
# ~/.cache/pip, ~/.cache/uv, ~/.cache/wandb, etc.). Project-specific
# subdirs (e.g. ~/.cache/<your-project>/) stay tracked.
ignore_library_caches = true
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

[registration]
# Default roar register/put to public visibility unless overridden by --private
public_by_default = false

# Publication scope for this repo is UNSET by default, so it resolves at publish
# time: `roar register`/`roar put` publish PRIVATE when you're signed in, and
# anonymous (public, unattributed) when you're not. Set it explicitly to override:
#   roar scope use private | public | <owner>/<project>

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

[registration.tagging]
# Create git tag on successful registration
enabled = true

[hash]
# Primary hash algorithm (blake3, sha256, sha512, md5)
primary = "blake3"
# Additional algorithms for roar get
get = ["sha256"]
# Additional algorithms for roar put/upload
put = []
# Additional algorithms for roar run
run = []

[proxy]
# Enable S3 proxy for lineage tracking during roar run
enabled = false

[tracer]
# Default tracer backend policy (auto, ebpf, preload, ptrace)
default = "auto"
# Allow runtime fallback to another tracer backend
fallback_enabled = true

[telemetry]
# Enable anonymous product telemetry for this project.
# Disable globally with `roar telemetry --disable` or use DO_NOT_TRACK=1 / ROAR_NO_TELEMETRY=1.
enabled = true
# Optional upload endpoint override. When unset, roar derives this from [glaas].url.
# endpoint = "https://api.glaas.ai/api/v1/telemetry/roar"
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
"""


def build_default_config_template() -> str:
    sections = [_CORE_CONFIG_TEMPLATE_PREFIX.rstrip()]
    sections.extend(template for template in iter_execution_backend_init_templates())
    sections.append(_CORE_CONFIG_TEMPLATE_SUFFIX.rstrip())
    return "\n\n".join(section for section in sections if section) + "\n"


DEFAULT_CONFIG_TEMPLATE = build_default_config_template()


def _gitignore_already_excludes_roar(content: str) -> bool:
    """Whether a .gitignore content string already lists `.roar` or `.roar/`.

    Per-line check (not substring) so entries like `not.roar` or commented
    lines don't fool us into thinking `.roar/` is already ignored.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if line in {".roar", ".roar/"}:
            return True
    return False


def _ensure_gitignore_excludes_roar(repo_root: Path) -> tuple[str, Path]:
    """Make sure repo_root/.gitignore lists `.roar/`.

    Creates the file if missing, appends the entry if absent, otherwise
    leaves it alone. Returns (action, gitignore_path) where action is one
    of 'created', 'appended', or 'already_present'.
    """
    gitignore_path = repo_root / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(".roar/\n")
        return ("created", gitignore_path)

    content = gitignore_path.read_text()
    if _gitignore_already_excludes_roar(content):
        return ("already_present", gitignore_path)

    with gitignore_path.open("a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(".roar/\n")
    return ("appended", gitignore_path)


def _ensure_active_session(roar_dir: Path) -> None:
    """Guarantee the initialized project has an active session."""
    with create_database_context(roar_dir) as db_ctx:
        db_ctx.sessions.get_or_create_active()


def _record_init_telemetry(cwd: Path) -> None:
    from ...telemetry.hooks import record_action_trigger

    record_action_trigger("init", start_dir=cwd)


def init_project(cwd: Path) -> Path:
    """Create the minimal local roar project structure in ``cwd``."""
    roar_dir = cwd / ".roar"
    if roar_dir.exists():
        _ensure_active_session(roar_dir)
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

    _ensure_active_session(roar_dir)
    return roar_dir


@click.group("init", invoke_without_command=True)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="No-op: .gitignore is updated by default. Kept for backward compatibility.",
)
@click.option(
    "--no",
    "-n",
    "--no-gitignore",
    "no_gitignore",
    is_flag=True,
    default=False,
    help="Don't touch .gitignore.",
)
@click.option(
    "--path",
    "init_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Initialize roar in the given directory instead of the current directory.",
)
@click.pass_context
def init(click_ctx: click.Context, yes: bool, no_gitignore: bool, init_path: Path | None) -> None:
    """Initialize roar in current directory.

    Creates a .roar directory for storing tracking data, a config.toml
    with default settings, and updates .gitignore to exclude .roar/.

    \b
    Examples:

        roar init                  # Initialize roar, ensure .gitignore excludes .roar/

        roar init --no-gitignore   # Initialize but leave .gitignore alone

        roar init --path /some/dir # Initialize in a specific directory

        roar init agents           # Install agent-facing guidance (skill + AGENTS.md)
    """
    del yes  # accepted for backward compatibility; default behavior matches it
    if click_ctx.invoked_subcommand is not None:
        return

    ctx: RoarContext = click_ctx.obj
    cwd = init_path if init_path is not None else ctx.cwd
    target_repo_root = RoarContext._get_repo_root(cwd)

    roar_dir = cwd / ".roar"
    roar_dir_existed = roar_dir.exists()

    init_project(cwd)

    _print_version_header()

    if roar_dir_existed:
        click.echo(f".roar directory already exists at {roar_dir}")
        return

    # Run the .gitignore step first so we can fold its status into the
    # single "Initialized roar" summary block instead of trailing prose.
    gitignore_status: str | None = None
    gitignore_action: str | None = None
    if target_repo_root is not None and not no_gitignore:
        gitignore_action, _ = _ensure_gitignore_excludes_roar(target_repo_root)
        if gitignore_action == "created":
            gitignore_status = "created .gitignore with .roar/ entry"
        elif gitignore_action == "appended":
            gitignore_status = "added .roar/ entry"
        else:
            gitignore_status = "already excluded"
    elif no_gitignore:
        gitignore_status = "skipped (--no-gitignore)"

    _print_init_summary(
        roar_dir=roar_dir,
        gitignore_status=gitignore_status,
        in_git_repo=target_repo_root is not None,
    )
    _maybe_print_init_hints(
        in_git_repo=target_repo_root is not None,
        gitignore_action=gitignore_action,
    )
    _record_init_telemetry(cwd)


def _print_init_summary(*, roar_dir: Path, gitignore_status: str | None, in_git_repo: bool) -> None:
    """One factual, terse block of what was created."""
    click.echo(f"Initialized roar in {roar_dir.parent}")
    click.echo(f"  database:   {roar_dir / 'roar.db'}")
    click.echo(f"  config:     {roar_dir / 'config.toml'}")
    click.echo("  scope:      unset (private when signed in, else anonymous)")
    if gitignore_status:
        click.echo(f"  gitignore:  {gitignore_status}")
    if not in_git_repo:
        click.echo("  git:        (not in a git repo)")


def _print_version_header() -> None:
    """First line of init: brand banner in green."""
    from .._format import print_brand_header

    print_brand_header("init")


def _maybe_print_init_hints(*, in_git_repo: bool, gitignore_action: str | None) -> None:
    """Print git-style `hint:` lines for next steps. Amber-colored to
    match git's hint convention. Suppressed in quiet/non-TTY contexts."""
    from ...version_check import upgrade_hint_text
    from .._format import hints_should_print, make_hint_printer

    if not hints_should_print():
        return

    _caps, hint = make_hint_printer()

    click.echo("")
    hint("Get started:")
    hint()
    hint("  roar run python train.py     # track inputs, outputs, env, commit")
    hint("  roar dag                     # view the lineage graph")
    hint("  roar register output.csv     # publish to GLaaS for teammates")
    hint()
    hint("Privacy:")
    hint("  Signed in -> `roar register`/`roar put` publish PRIVATE by default.")
    hint("  Not signed in -> public + anonymous (run `roar login` to keep runs private).")
    hint("  Set an explicit scope with `roar scope use private|public|<owner>/<project>`.")
    hint("  Anonymous public publishing prompts for confirmation;")
    hint("  bypass with `roar register -y` or `roar put -y`.")
    hint()
    hint("Tracer auto-selects (eBPF → preload → ptrace). Switch with `roar tracer <backend>`;")
    hint("see all backends and readiness with `roar tracer`.")
    if in_git_repo:
        hint()
        hint("`roar run` requires a clean git tree — runs are tagged with the commit SHA.")
    else:
        hint()
        hint("Not in a git repo: `roar run` still works and captures lineage,")
        hint("but runs won't be tagged with a commit. Run inside a git repo")
        hint("(code committed) to anchor lineage so registered runs are")
        hint("reproducible from source.")
    if gitignore_action in ("created", "appended"):
        hint()
        hint("Commit the .gitignore change before your first `roar run`:")
        hint("  git add .gitignore && git commit -m 'ignore .roar/'")

    upgrade = upgrade_hint_text()
    if upgrade:
        hint()
        hint(upgrade)

    hint()
    hint("Docs: https://glaas.ai/docs")
    hint("Disable these hints with `roar config set hints.enabled false`.")


# Register subcommands. Imported here (not at top of file) so the heavier
# commands above keep their lazy import behavior unaffected.
from .init_agents import init_agents as _init_agents  # noqa: E402

init.add_command(_init_agents)
