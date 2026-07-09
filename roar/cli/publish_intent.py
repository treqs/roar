from __future__ import annotations

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class PublishIntent:
    public: bool
    anonymous: bool
    used_public_default: bool = False
    # Not logged in and no scope set: fell back to anonymous because private
    # publishing needs an account. Drives the "you're publishing anonymously" warning.
    defaulted_anonymous: bool = False


def _is_logged_in() -> bool:
    """True iff there's a usable GLaaS/TReqs session on this machine."""
    try:
        from ..auth_store import load_auth_state

        auth = load_auth_state()
    except Exception:
        return False
    return auth is not None and bool(auth.access_token)


def resolve_publish_intent(
    public: bool | None,
    anonymous: bool,
    *,
    start_dir: str | None = None,
) -> PublishIntent:
    """Resolve visibility and attribution for publish-style commands.

    Explicit choices win (``--anonymous`` / ``--public`` / ``--private``, then an
    explicitly-set repo scope). When scope is *unset* the default is resolved
    LIVE rather than frozen at ``roar init`` time:

    - ``registration.public_by_default`` set  -> public (explicit global pref);
    - else logged in -> **private** (don't expose by accident);
    - else (not logged in) -> **anonymous + warn** (private needs an account).

    Deterministic, so it's headless-safe — no interactive prompt is required to
    reach a default.
    """
    if anonymous:
        return PublishIntent(public=True, anonymous=True)

    if public is not None:
        return PublishIntent(public=public, anonymous=False)

    from ..scope_config import load_repo_scope

    scope = load_repo_scope(start_dir)
    if scope is not None:
        if scope.mode == "anonymous":
            return PublishIntent(public=True, anonymous=True)
        if scope.mode == "public":
            return PublishIntent(public=True, anonymous=False)
        if scope.mode in {"private", "project"}:
            return PublishIntent(public=False, anonymous=False)

    # Scope unset: resolve the default from current state, not a value baked at init.
    from ..integrations.config import config_get

    if bool(config_get("registration.public_by_default", start_dir=start_dir)):
        return PublishIntent(public=True, anonymous=False, used_public_default=True)
    if _is_logged_in():
        return PublishIntent(public=False, anonymous=False)
    return PublishIntent(public=True, anonymous=True, defaulted_anonymous=True)


def warn_public_default() -> None:
    """Tell the user when config caused public visibility."""
    click.echo(
        "Warning: defaulting to public visibility because "
        "registration.public_by_default=true in roar config. Pass --private to override.",
        err=True,
    )


def warn_defaulted_anonymous() -> None:
    """Tell the user a not-logged-in publish is going out anonymously and publicly."""
    click.echo(
        "Warning: not signed in — publishing anonymously and publicly (anyone with the "
        "hash can read it).",
        err=True,
    )
    click.echo(
        "  `roar login` to keep runs private by default, or set a scope with "
        "`roar scope use <owner>/<project>`.",
        err=True,
    )


def _preview_hash(value: str) -> str:
    """Shorten hashes in confirmation prompts (mirrors register.py's _preview_hash)."""
    return f"{value[:12]}..." if len(value) > 12 else value


def _confirm_or_explain_noninteractive(prompt: str, *, default: bool, command_name: str) -> bool:
    """``click.confirm()``, but turns a non-interactive EOF into an actionable error.

    Without this, ``click.confirm()`` on closed/absent stdin (the normal case for
    workflow-orchestrated automation — e.g. a treqs-agent job subprocess with no
    terminal attached) raises ``click.Abort`` on EOF, which Click's default
    top-level handling turns into a bare "Aborted!" with no indication of *why*
    or what to do about it. This still fails fast (it never silently proceeds
    past an unanswered prompt) but tells the operator to pass ``-y`` instead of
    leaving them to guess. Deliberately keyed off catching the actual EOF
    ``click.confirm()`` raises rather than checking ``sys.stdin.isatty()`` up
    front — Click's own ``CliRunner`` test harness feeds simulated input through
    a non-tty stream, so an ``isatty()`` precheck would misfire on real prompt
    tests, not just genuine non-interactive automation.
    """
    try:
        return click.confirm(prompt, default=default)
    except click.Abort:
        raise click.ClickException(
            "Refusing to proceed without confirmation in a non-interactive session "
            "(no input available on stdin). Pass "
            f"`{command_name} -y` to skip this confirmation in scripts, CI, or "
            "workflow automation."
        ) from None


def confirm_defaulted_active_session_publish(
    *, session_hash: str, command_name: str, start_dir: str | None = None
) -> bool:
    """Prompt before a target-less invocation publishes the whole active session.

    With no target, ``roar register`` publishes every job and artifact in the
    active session (the same scope ``roar status`` shows) rather than a single
    artifact's narrower upstream lineage — a materially bigger, easy-to-trigger-
    by-accident publish. This gate fires independent of ``publish_intent.anonymous``:
    an attributed, private publish of the wrong (too-broad) scope is still a real
    mistake, so it can't rely on the anonymous/public confirmation to catch it.
    """
    click.echo("")
    click.echo(f"Will publish to: {_publish_url_preview(start_dir, session_hash)}")
    click.echo(
        f"No target given — the whole active session ({_preview_hash(session_hash)}) will be "
        "published, including every job and artifact recorded in it so far."
    )
    click.echo(f"Use `{command_name} -y` to skip this confirmation in scripts.")
    click.echo("")
    return _confirm_or_explain_noninteractive(
        "Publish the whole active session?", default=False, command_name=command_name
    )


def confirm_anonymous_public_publish(*, command_name: str, start_dir: str | None = None) -> bool:
    """Prompt before an anonymous public publication.

    Renders a "Will publish to:" preview line above the prompt so the user
    sees the destination URL pattern before committing — mirroring what
    ``--dry-run`` already shows. The session-hash component is a
    placeholder (the real hash isn't known until registration runs), but
    the host + path shape is the actionable trust signal.
    """
    click.echo("")
    click.echo(f"Will publish to: {_publish_url_preview(start_dir)}")
    click.echo("Anonymous scope publishes publicly without account attribution.")
    click.echo("Anyone with the GLaaS record hash can read this lineage.")
    click.echo(f"Use `{command_name} -y` to skip this confirmation in scripts.")
    click.echo("")
    return click.confirm("Publish anonymously and publicly?", default=False)


def _publish_url_preview(start_dir: str | None, session_hash: str | None = None) -> str:
    """Return the dag URL using the configured GLaaS host.

    ``session_hash`` is the real, already-resolved hash when the caller has one
    (e.g. the defaulted-active-session gate, which runs after target resolution).
    Falls back to a ``<session-hash>`` placeholder when the real hash isn't known
    yet (e.g. the anonymous-publish gate, which runs before registration).
    """
    from ..integrations.config.raw import get_raw_glaas_web_url

    web_url = get_raw_glaas_web_url(start_dir=start_dir) or "https://glaas.ai"
    return f"{web_url}/dag/{session_hash or '<session-hash>'}"
