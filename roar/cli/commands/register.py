"""
Native Click implementation of the register command.

Usage: roar register [options] [target]

Registers artifact, job, step, or session lineage with GLaaS. Without a target,
registers the current active session.
"""

import json

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


def _resolve_public_flag(public: bool | None, *, start_dir: str | None = None) -> tuple[bool, bool]:
    """Resolve publish visibility and whether public came from config default."""
    if public is not None:
        return public, False

    from ...integrations.config import config_get

    resolved_public = bool(config_get("registration.public_by_default", start_dir=start_dir))
    return resolved_public, resolved_public


def _warn_public_default() -> None:
    """Tell the user when config caused public visibility."""
    click.echo(
        "Warning: defaulting to public visibility because "
        "registration.public_by_default=true in roar config. Pass --private to override.",
        err=True,
    )


def _confirm_secrets(detected_secrets: list[str]) -> bool:
    """Prompt user to confirm registration with secrets."""
    click.echo("")
    click.echo(f"Detected {len(detected_secrets)} potential secret type(s) that will be redacted:")
    for secret_type in detected_secrets:
        click.echo(f"  - {secret_type}")
    click.echo("")
    return click.confirm("Continue with registration? (secrets will be filtered)", default=False)


@click.command("register")
@click.argument("target", type=click.STRING, required=False)
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
    "--public/--private",
    "public",
    default=None,
    help=(
        "Submit as public or private lineage. When omitted, roar uses "
        "registration.public_by_default from config. Public allows anonymous or "
        "attributed submission; private requires attributed submission."
    ),
)
@click.option(
    "--anonymous",
    is_flag=True,
    help="Force public anonymous registration even when local GLaaS auth is configured.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable JSON and suppress the human summary.",
)
@click.option(
    "--no-tag",
    is_flag=True,
    help="Do not create/update a local roar git tag after successful registration.",
)
@click.pass_obj
@require_init
def register(
    ctx: RoarContext,
    target: str | None,
    dry_run: bool,
    yes: bool,
    as_blake3: bool,
    public: bool | None,
    anonymous: bool,
    json_output: bool,
    no_tag: bool,
) -> None:
    """Register lineage with GLaaS.

    Submits lineage to the GLaaS server, starting from one of:
    - an artifact path or tracked artifact hash
    - a local job UID/hash
    - a DAG step reference like ``@4``
    - a local session hash/prefix previously shown by roar

    Artifact paths must refer to files tracked by roar.

    Visibility / attribution matrix:
    - effective private -> private + attributed only
    - effective public -> public + anonymous OR public + attributed
    - --anonymous -> public + anonymous, ignoring configured auth
    - private + anonymous is not allowed

    Effective visibility comes from `--public` / `--private` when provided,
    otherwise from `registration.public_by_default` in roar config. `--anonymous`
    forces public visibility.

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
    if anonymous and public is False:
        raise click.ClickException("--anonymous requires public visibility; remove --private.")

    if anonymous:
        resolved_public, used_public_default = True, False
    else:
        resolved_public, used_public_default = _resolve_public_flag(public, start_dir=str(ctx.cwd))
    if used_public_default:
        _warn_public_default()

    response = register_lineage_target(
        RegisterLineageRequest(
            target=target,
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
            dry_run=dry_run,
            as_blake3=as_blake3,
            public=resolved_public,
            anonymous=anonymous,
            skip_confirmation=yes,
            confirm_callback=_confirm_secrets if not yes else None,
            no_tag=no_tag,
        )
    )

    if not response.success:
        if response.aborted_by_user:
            click.echo("Registration aborted.")
            raise SystemExit(1)
        raise click.ClickException(response.error or "Registration failed")

    if json_output:
        click.echo(
            json.dumps(
                {
                    "success": True,
                    "session_hash": response.session_hash,
                    "session_url": f"/sessions/{response.session_hash}"
                    if response.session_hash
                    else None,
                    "jobs_registered": response.jobs_registered,
                    "artifacts_registered": response.artifacts_registered,
                    "links_created": response.links_created,
                    "artifact_hash": response.artifact_hash or None,
                    "dry_run": dry_run,
                    "secrets_detected": list(response.secrets_detected),
                    "secrets_redacted": response.secrets_redacted,
                    "error": response.error,
                },
                sort_keys=True,
            )
        )
        return

    web_url = _resolve_glaas_web_url(start_dir=str(ctx.cwd))
    session_preview = _preview_hash(response.session_hash) if response.session_hash else ""
    display_target = target or "current session"

    # Format output
    if dry_run:
        click.echo(f"Dry run: would register lineage for: {display_target}")
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
        click.echo(f"Registered lineage for: {display_target}")
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
