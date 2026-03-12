"""
Native Click implementation of the register command.

Usage: roar register [options] <target>

Registers artifact, job, step, or session lineage with GLaaS.
"""

import click

from ...config import config_get
from ...services.registration.register_service import RegisterService
from ..context import RoarContext
from ..decorators import require_init


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
@click.pass_obj
@require_init
def register(ctx: RoarContext, target: str, dry_run: bool, yes: bool, as_blake3: bool) -> None:
    """Register lineage with GLaaS.

    Submits lineage to the GLaaS server, starting from one of:
    - an artifact path or tracked artifact hash
    - a local job UID/hash
    - a DAG step reference like ``@4``
    - a local session hash/prefix previously shown by roar

    Artifact paths must refer to files tracked by roar.

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
    # Create service
    service = RegisterService()

    # Register the requested lineage target
    result = service.register_lineage_target(
        target=target,
        roar_dir=ctx.roar_dir,
        cwd=ctx.cwd,
        dry_run=dry_run,
        as_blake3=as_blake3,
        skip_confirmation=yes,
        confirm_callback=_confirm_secrets if not yes else None,
    )

    if not result.success:
        if result.aborted_by_user:
            click.echo("Registration aborted.")
            raise SystemExit(1)
        raise click.ClickException(result.error or "Registration failed")

    web_url = config_get("glaas.web_url") or "https://glaas.ai"

    # Format output
    if dry_run:
        click.echo("Dry run - would register:")
        click.echo(f"  Session: {result.session_hash[:12]}...")
        click.echo(f"  Jobs: {result.jobs_registered}")
        click.echo(f"  Artifacts: {result.artifacts_registered}")
        click.echo(f"  Links: {result.links_created}")
        if result.secrets_detected:
            click.echo(f"  Secrets to redact: {len(result.secrets_detected)} types")
        click.echo("")
        click.echo("View on GLaaS:")
        click.echo(f"  Session:  {web_url}/dag/{result.session_hash}")
        if result.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{result.artifact_hash}")
    else:
        click.echo(f"Registered lineage for: {target}")
        click.echo(f"  Session: {result.session_hash[:12]}...")
        click.echo(f"  Jobs: {result.jobs_registered}")
        click.echo(f"  Artifacts: {result.artifacts_registered}")
        click.echo(f"  Links: {result.links_created}")
        if result.secrets_redacted:
            click.echo(f"  Secrets redacted: {len(result.secrets_detected)} types")

        if result.error:
            click.echo("")
            click.echo("Registration completed with errors:", err=True)
            # Split multi-error strings into separate lines for readability
            for error in result.error.split("; "):
                click.echo(f"  - {error}", err=True)

        if result.artifact_hash:
            click.echo("")
            click.echo("To reproduce this artifact:")
            click.echo(f"  roar reproduce {result.artifact_hash}")

        click.echo("")
        click.echo("View on GLaaS:")
        click.echo(f"  Session:  {web_url}/dag/{result.session_hash}")
        if result.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{result.artifact_hash}")
