"""
Native Click implementation of the register command.

Usage: roar register [options] <target>

Registers artifact, job, step, or session lineage with GLaaS.
"""

import json

import click

from ...application.publish.requests import RegisterLineageRequest
from ...application.publish.results import RegisterLineageResponse, RegisterTagSummary
from ...application.publish.service import register_lineage_target
from ..context import RoarContext
from ..decorators import require_init
from ..publish_intent import (
    confirm_anonymous_public_publish,
    resolve_publish_intent,
    visibility_label,
    warn_defaulted_anonymous,
    warn_public_default,
)


def _preview_hash(value: str) -> str:
    """Shorten hashes in command summaries."""
    return f"{value[:12]}..." if len(value) > 12 else value


def _format_jobs_line(response: RegisterLineageResponse) -> str:
    """Render the Jobs summary count, calling out already-registered jobs.

    On a re-register the jobs already exist under the session, so newly-created
    count is 0; surface the rest as "0 (4 already registered)" instead of an
    ambiguous "4".
    """
    if response.jobs_existing:
        return f"{response.jobs_registered} ({response.jobs_existing} already registered)"
    return str(response.jobs_registered)


def _resolve_glaas_web_url(*, start_dir: str | None = None) -> str:
    """Load the GLaaS web URL with the lightweight preview config path."""
    from ...integrations.config.raw import get_raw_glaas_web_url

    return get_raw_glaas_web_url(start_dir=start_dir) or "https://glaas.ai"


def _display_session_url(response_session_url: str | None, web_url: str, session_hash: str) -> str:
    """Prefer GLaaS' returned URL and fall back to the legacy local URL shape."""
    if response_session_url:
        return response_session_url
    return f"{web_url}/dag/{session_hash}"


def _signup_nudge_marker():
    """Once-per-user marker path for the GLaaS signup nudge, or None on error."""
    try:
        from ...telemetry.paths import resolve_paths

        return resolve_paths().cache_dir / "signup_nudge.json"
    except Exception:
        return None


def _maybe_show_signup_nudge() -> None:
    """After a *first* anonymous register, name what a GLaaS account unlocks.

    Anonymous public register succeeds with no account, so nothing otherwise
    gives the user a reason to sign up — the value they came for (local DAG +
    reproducibility) is already theirs. This names the account-only
    capabilities (the local-vs-GLaaS difference) and the exact next command.

    Fires once per user (a cache-dir marker so it isn't repeated every run)
    and only when hints are enabled. Fail-open and silent on any error.
    """
    from .._format import hints_should_print, make_hint_printer

    if not hints_should_print():
        return
    marker = _signup_nudge_marker()
    if marker is not None and marker.exists():
        return

    _caps, hint = make_hint_printer()
    hint("This lineage is public and unattributed (no account needed).")
    hint("A GLaaS account lets you keep runs private, share them with your team,")
    hint("and reproduce them on another machine — sign in with `roar login`.")

    if marker is not None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"shown": True}), encoding="utf-8")
        except Exception:
            pass


def _current_login_name() -> str | None:
    """Username/email of the active GLaaS session, or None if not logged in."""
    try:
        from ...auth_store import load_auth_state

        auth = load_auth_state()
    except Exception:
        return None
    if auth is None or not auth.access_token:
        return None
    return auth.user.username or auth.user.email or "your account"


def _maybe_show_attribution_nudge(login_name: str) -> None:
    """Authenticated, but the lineage published anonymously — nudge to attribute.

    The inverse of the signup nudge: the user already has an account, so being
    published unattributed is probably not what they want. Say how to fix it."""
    from .._format import hints_should_print, make_hint_printer

    if not hints_should_print():
        return
    _caps, hint = make_hint_printer()
    hint(f"Signed in as {login_name}, but this lineage was published anonymously (unattributed).")
    hint("Attribute future runs with `roar scope use <owner>/<project>` (or `register --public`).")


def _render_tag_summary(summary: RegisterTagSummary | None) -> None:
    """Render the P1-23 tag-push block above the main register output.

    Tells the user exactly which commits got tagged and where the tags
    were pushed (or why the push was skipped). Stays silent if there's
    nothing to say (tagging was disabled for the whole flow).
    """
    if summary is None:
        return
    if summary.session_tag is None and not summary.job_tags:
        return  # tagging disabled / no commits — nothing to show

    if summary.session_tag:
        click.echo(f"Tagged session commit as {summary.session_tag}")
    if summary.job_tags:
        # Make multi-commit obvious — both that there are more, and
        # exactly which ones.
        click.echo(
            f"Tagged {len(summary.job_tags)} additional job "
            f"commit{'s' if len(summary.job_tags) != 1 else ''}: "
            f"{', '.join(summary.job_tags)}"
        )

    pushed_count = (1 if summary.session_tag else 0) + len(summary.job_tags)
    if summary.remote:
        word = "tag" if pushed_count == 1 else "tags"
        click.echo(f"Pushed {pushed_count} {word} to {summary.remote}")
    elif summary.push_skipped_reason == "never_config":
        click.echo(
            "Note: roar tags NOT pushed (git.push_tags_on_register=never).",
            err=True,
        )
        click.echo(
            "GLaaS links to these tags will not resolve for teammates.",
            err=True,
        )
    elif summary.push_skipped_reason == "no_remote":
        click.echo(
            "Note: roar tags created locally but NOT pushed (no git remote).",
            err=True,
        )
        click.echo(
            "Add a remote (`git remote add origin <url>`) and re-register so "
            "teammates can resolve the commit.",
            err=True,
        )
    click.echo("")


def _confirm_secrets(detected_secrets: list[str]) -> bool:
    """Prompt user to confirm registration with secrets."""
    click.echo("")
    click.echo(f"Detected {len(detected_secrets)} potential secret type(s) that will be redacted:")
    for secret_type in detected_secrets:
        click.echo(f"  - {secret_type}")
    click.echo("")
    return click.confirm("Continue with registration? (secrets will be filtered)", default=False)


def _register_notes(
    response: RegisterLineageResponse, *, on_glaas: bool, visibility: str | None = None
) -> dict[str, str]:
    """Operational receipt details folded onto the reproducibility punchlist.

    Each becomes the indented note under its check, so the one checklist also
    shows what register *did* (tagged which commit, pushed where, what landed on
    GLaaS) — the punchlist-with-details-below style."""
    notes: dict[str, str] = {}
    ts = response.tag_summary
    if ts and ts.session_tag:
        extra = len(ts.job_tags)
        notes["committed"] = f"tagged {ts.session_tag}" + (
            f" (+{extra} job commit{'s' if extra != 1 else ''})" if extra else ""
        )
    if ts and ts.remote:
        notes["pushed"] = f"pushed to {ts.remote}"
    if on_glaas:
        recorded = (
            f"{_format_jobs_line(response)} jobs · {response.artifacts_registered} artifacts · "
            f"{response.links_created} links"
        )
        if response.labels_synced:
            recorded += f" · {response.labels_synced} labels"
        # Lead with visibility + account so the receipt confirms what was exposed.
        notes["on_glaas"] = f"{visibility} · {recorded}" if visibility else recorded
    return notes


def _render_register_checklist(
    ctx: RoarContext,
    target: str,
    response: RegisterLineageResponse,
    *,
    on_glaas: bool,
    dry_run: bool = False,
    visibility: str | None = None,
) -> None:
    """Render the shared reproducibility punchlist as register's receipt.

    The single checklist consolidates the old scattered warnings AND the
    operational summary (tag/push/counts, folded in as notes), evaluated the
    same way `roar reproduce` shows it. On a dry run nothing is published, so the
    publish check is shown as n/a (not a failure). Warn, never block; best-effort
    (any failure here must not break registration)."""
    try:
        from ...application.reproducibility.report import (
            build_report,
            render_report,
            unsourced_input_paths,
        )

        report = build_report(
            committed=response.reproducible,
            pushed=bool(response.tag_summary and response.tag_summary.remote),
            # Anything reaching `register` went through `roar run`, which always
            # captures the runtime — so treat it as recorded (best-effort).
            runtime_ok=True,
            unsourced_paths=unsourced_input_paths(ctx.roar_dir, ctx.cwd, target),
            on_glaas=on_glaas,
            # Computed from the session's commit span (matches `roar reproduce`);
            # the old job-tags proxy mis-read single-commit whenever tagging was
            # skipped (e.g. no remote), contradicting reproduce's verdict.
            single_commit=response.single_commit,
            notes=_register_notes(response, on_glaas=on_glaas, visibility=visibility),
            na=(
                {"on_glaas": f"dry run — would publish {visibility}"}
                if dry_run and visibility
                else {"on_glaas": "dry run — nothing published yet"}
                if dry_run
                else None
            ),
        )
    except Exception:
        return

    click.echo("")
    for line in render_report(report, title="Reproducibility").splitlines():
        click.echo(line)


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
    help=("Skip confirmation prompts for anonymous public publishing and secret filtering."),
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
@click.pass_obj
@require_init
def register(
    ctx: RoarContext,
    target: str,
    dry_run: bool,
    yes: bool,
    as_blake3: bool,
    public: bool | None,
    anonymous: bool,
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
    otherwise from this repo's `roar scope` setting. `--anonymous` forces
    public visibility.

    If secrets are detected in the data (API keys, tokens, passwords, etc.),
    you will be prompted to confirm. Use --yes to skip the prompt and
    automatically proceed with secret redaction. If the effective scope is
    anonymous, you will also be prompted before publishing public anonymous
    lineage unless --yes is provided.

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

    publish_intent = resolve_publish_intent(
        public,
        anonymous,
        start_dir=str(ctx.cwd),
    )
    if publish_intent.used_public_default:
        warn_public_default()
    if publish_intent.defaulted_anonymous:
        warn_defaulted_anonymous()

    if (
        publish_intent.anonymous
        and not yes
        and not dry_run
        and not confirm_anonymous_public_publish(
            command_name="roar register", start_dir=str(ctx.cwd)
        )
    ):
        click.echo("Registration aborted.")
        raise SystemExit(1)

    response = register_lineage_target(
        RegisterLineageRequest(
            target=target,
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
            dry_run=dry_run,
            as_blake3=as_blake3,
            public=publish_intent.public,
            anonymous=publish_intent.anonymous,
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
    session_url = _display_session_url(response.session_url, web_url, response.session_hash)
    visibility = visibility_label(publish_intent)

    # Format output
    if dry_run:
        click.echo(f"Dry run: would register lineage for: {target}")
        click.echo(f"  Session: {session_preview}")
        click.echo(f"  Jobs: {response.jobs_registered}")
        click.echo(f"  Artifacts: {response.artifacts_registered}")
        click.echo(f"  Links: {response.links_created}")
        if response.secrets_detected:
            click.echo(f"  Secrets to redact: {len(response.secrets_detected)} types")
        # Preview reproducibility BEFORE publishing (not yet on GLaaS).
        _render_register_checklist(ctx, target, response, on_glaas=False, dry_run=True, visibility=visibility)
        click.echo("")
        click.echo("GLaaS:")
        click.echo(f"  Session:  {session_url}")
        if response.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{response.artifact_hash}")
    elif response.already_registered:
        # The whole lineage was already on GLaaS — a no-op re-register. Say so
        # plainly (it's not a fresh publish) and surface any label updates.
        for warning in response.warnings:
            click.echo(f"Warning: {warning}", err=True)
        _render_tag_summary(response.tag_summary)
        click.echo(f"Already registered on GLaaS: {target}")
        click.echo(f"  Session: {session_preview}")
        click.echo(f"  Labels: {response.labels_synced}")
        click.echo("")
        click.echo("GLaaS:")
        click.echo(f"  Session:  {session_url}")
    else:
        for warning in response.warnings:
            click.echo(f"Warning: {warning}", err=True)
        click.echo(f"Registered lineage for: {target}")
        click.echo(f"  Session: {session_preview}")
        if response.secrets_redacted:
            click.echo(f"  Secrets redacted: {len(response.secrets_detected)} types")

        if response.error:
            click.echo("")
            click.echo("Registration completed with errors:", err=True)
            # Split multi-error strings into separate lines for readability
            for error in response.error.split("; "):
                click.echo(f"  - {error}", err=True)

        # One punchlist: reproducibility checks + what register did (tag/push/
        # counts folded in as notes), replacing the old separate stat + tag block.
        _render_register_checklist(ctx, target, response, on_glaas=True, visibility=visibility)

        click.echo("")
        click.echo("GLaaS:")
        click.echo(f"  Session:  {session_url}")
        if response.artifact_hash:
            click.echo(f"  Artifact: {web_url}/artifact/{response.artifact_hash}")
            click.echo("")
            click.echo("Next:")
            click.echo(f"  roar show --artifact {response.artifact_hash}")
            click.echo(f"  roar reproduce {response.artifact_hash}")

    if not dry_run:
        from ...telemetry.hooks import record_action_trigger

        record_action_trigger("register", start_dir=ctx.cwd)

        # Anonymous register. If the user has NO account, nudge them to sign up
        # (names what an account unlocks). If they're already signed in, the
        # useful nudge is the opposite — they published unattributed, so show
        # how to attribute.
        if publish_intent.anonymous:
            login_name = _current_login_name()
            if login_name:
                _maybe_show_attribution_nudge(login_name)
            else:
                _maybe_show_signup_nudge()
