"""
Native Click implementation of the register command.

Usage: roar register [options] <target>

Registers artifact, job, step, or session lineage with GLaaS.
"""

import click

from ...application.publish.requests import RegisterLineageRequest
from ...application.publish.service import register_lineage_target
from ..context import RoarContext
from ..decorators import require_init


def _preview_hash(value: str) -> str:
    """Shorten hashes in command summaries."""
    return f"{value[:12]}..." if len(value) > 12 else value


def _resolve_glaas_web_url(*, start_dir: str | None = None) -> str:
    """Load the GLaaS web URL with the lightweight preview config path."""
    from ...integrations.config.raw import get_raw_glaas_web_url

    return get_raw_glaas_web_url(start_dir=start_dir) or "https://glaas.ai"


def _confirm_secrets(detected_secrets: list[str]) -> bool:
    """Prompt user to confirm registration with secrets."""
    click.echo("")
    click.echo(f"Detected {len(detected_secrets)} potential secret type(s) that will be redacted:")
    for secret_type in detected_secrets:
        click.echo(f"  - {secret_type}")
    click.echo("")
    return click.confirm("Continue with registration? (secrets will be filtered)", default=False)


@click.command("register")
@click.argument("target", type=click.STRING)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview what would be registered without calling GLaaS API",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt and proceed with secret filtering",
)
@click.option(
    "--as-blake3",
    is_flag=True,
    help="Upgrade tracked S3 artifacts from ETag-only hashes to BLAKE3 before registration",
)
@click.option(
    "--public",
    is_flag=True,
    help=(
        "Submit as public lineage. --public allows public+anonymous or public+attributed "
        "submission; without it, non-public submission must be private+attributed."
    ),
)
@click.pass_obj
@require_init
def register(
    ctx: RoarContext,
    target: str,
    dry_run: bool,
    yes: bool,
    as_blake3: bool,
    public: bool,
) -> None:
    """Register lineage with GLaaS.

    Submits lineage to the GLaaS server, starting from one of:
    - an artifact path or tracked artifact hash
    - a local job UID/hash
    - a DAG step reference like ``@4``
    - a local session hash/prefix previously shown by roar

    Artifact paths must refer to files tracked by roar.

    Visibility / attribution matrix:
    - no --public -> private + attributed only
    - --public -> public + anonymous OR public + attributed
    - private + anonymous is not allowed

    If secrets are detected in the data (API keys, tokens, passwords, etc.),
    you will be prompted to confirm. Use --yes to skip the prompt and
    automatically proceed with secret redaction.

    \b
    Examples:

        roar register model.pt              # Register model lineage

        roar register --dry-run model.pt    # Preview without registering

        roar register -y model.pt           # Skip confirmation prompt

        roar register @4                    # Register the lineage for DAG step 4

        roar register deadbeef             # Register the lineage for a local job UID

        roar register 7f1e...c9a4          # Register the lineage for a tracked artifact hash

        roar register 8d7a1f2c...           # Register a whole local session

        roar register --as-blake3 model.pt  # Upgrade S3 etag hashes

        roar register outputs/metrics.json  # Register from subdirectory
    """
    response = register_lineage_target(
        RegisterLineageRequest(
            target=target,
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
            dry_run=dry_run,
            as_blake3=as_blake3,
            public=public,
            skip_confirmation=yes,
            confirm_callback=_confirm_secrets if not yes else None,
        )
    )

    if not response.success:
        if response.aborted_by_user:
            click.echo("Registration aborted.")
            raise SystemExit(1)
        raise click.ClickException(response.error or "Registration failed")

    web_url = _resolve_glaas_web_url(start_dir=str(ctx.cwd))
    session_preview = _preview_hash(response.session_hash) if response.session_hash else ""

    # Format output
    if dry_run:
        click.echo(f"Dry run: would register lineage for: {target}")
        click.echo(f"  Session: {session_preview}")
        click.echo(f"  Jobs: {response.jobs_registered}")
        click.echo(f"  Artifacts: {response.artifacts_registered}")
        click.echo(f"  Links: {response.links_created}")
        if response.secrets_detected:
            click.echo(f"  Secrets to redact: {len(response.secrets_detected)} types")
        click.echo("")
        click.echo("GLaaS:")
        click.echo(f"  Session:  {web_url}/dag/{response.session_hash}")
        if response.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{response.artifact_hash}")
    else:
        click.echo(f"Registered lineage for: {target}")
        click.echo(f"  Session: {session_preview}")
        click.echo(f"  Jobs: {response.jobs_registered}")
        click.echo(f"  Artifacts: {response.artifacts_registered}")
        click.echo(f"  Links: {response.links_created}")
        if response.secrets_redacted:
            click.echo(f"  Secrets redacted: {len(response.secrets_detected)} types")

        if response.error:
            click.echo("")
            click.echo("Registration completed with errors:", err=True)
            # Split multi-error strings into separate lines for readability
            for error in response.error.split("; "):
                click.echo(f"  - {error}", err=True)

        click.echo("")
        click.echo("GLaaS:")
        click.echo(f"  Session:  {web_url}/dag/{response.session_hash}")
        if response.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{response.artifact_hash}")
            click.echo("")
            click.echo("Next:")
            click.echo(f"  roar show --artifact {response.artifact_hash}")
            click.echo(f"  roar reproduce {response.artifact_hash}")
