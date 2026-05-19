from __future__ import annotations

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class PublishIntent:
    public: bool
    anonymous: bool
    used_public_default: bool = False


def resolve_publish_intent(
    public: bool | None,
    anonymous: bool,
    *,
    start_dir: str | None = None,
) -> PublishIntent:
    """Resolve visibility and attribution for publish-style commands."""
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

    from ..integrations.config import config_get

    resolved_public = bool(config_get("registration.public_by_default", start_dir=start_dir))
    return PublishIntent(
        public=resolved_public,
        anonymous=False,
        used_public_default=resolved_public,
    )


def warn_public_default() -> None:
    """Tell the user when config caused public visibility."""
    click.echo(
        "Warning: defaulting to public visibility because "
        "registration.public_by_default=true in roar config. Pass --private to override.",
        err=True,
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


def _publish_url_preview(start_dir: str | None) -> str:
    """Return the dag URL pattern using the configured GLaaS host."""
    from ..integrations.config.raw import get_raw_glaas_web_url

    web_url = get_raw_glaas_web_url(start_dir=start_dir) or "https://glaas.ai"
    return f"{web_url}/dag/<session-hash>"
