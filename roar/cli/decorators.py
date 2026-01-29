"""
Click decorators for roar CLI commands.

Provides requirement decorators that validate preconditions before
command execution:
- require_init: Ensures roar is initialized (.roar directory exists)
- require_git: Ensures we're in a git repository
- require_clean_git: Ensures git working tree has no uncommitted changes
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import click

if TYPE_CHECKING:
    from .context import RoarContext

F = TypeVar("F", bound=Callable[..., Any])


def require_init(f: F) -> F:
    """Decorator to require roar initialization.

    Commands decorated with this will fail with a helpful error message
    if the .roar directory does not exist.

    Usage:
        @cli.command()
        @click.pass_obj
        @require_init
        def status(ctx: RoarContext):
            ...

    Note:
        This decorator should be applied AFTER @click.pass_obj so that
        the RoarContext is available.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Get ctx from first positional arg (from @click.pass_obj)
        ctx_maybe: Any = args[0] if args else kwargs.get("ctx")

        if ctx_maybe is None:
            raise click.ClickException(
                "Internal error: RoarContext not available. "
                "Ensure @click.pass_obj is applied before @require_init."
            )
        ctx: RoarContext = ctx_maybe

        if not ctx.is_initialized:
            # Output to stdout to match legacy command behavior
            click.echo("Error: roar is not initialized in this directory.")
            click.echo("")
            click.echo("Run 'roar init' first to set up roar.")
            raise SystemExit(1)

        return f(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def require_git(f: F) -> F:
    """Decorator to require a git repository.

    Commands decorated with this will fail with a helpful error message
    if not running inside a git repository.

    Usage:
        @cli.command()
        @click.pass_obj
        @require_git
        def run(ctx: RoarContext, command: tuple[str, ...]):
            ...

    Note:
        This decorator should be applied AFTER @click.pass_obj so that
        the RoarContext is available.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Get ctx from first positional arg (from @click.pass_obj)
        ctx_maybe: Any = args[0] if args else kwargs.get("ctx")

        if ctx_maybe is None:
            raise click.ClickException(
                "Internal error: RoarContext not available. "
                "Ensure @click.pass_obj is applied before @require_git."
            )
        ctx: RoarContext = ctx_maybe

        if not ctx.has_repo:
            raise click.ClickException(
                "Not in a git repository.\n"
                "roar requires the working directory to be inside a git repository."
            )

        return f(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def require_clean_git(f: F) -> F:
    """Decorator to require a clean git working tree.

    Commands decorated with this will fail with a helpful error message
    if there are uncommitted changes in the git repository.

    This is important for provenance tracking - we need to know the exact
    state of the code that was run.

    Usage:
        @cli.command()
        @click.pass_obj
        @require_init
        @require_git
        @require_clean_git
        def run(ctx: RoarContext, command: tuple[str, ...]):
            ...

    Note:
        This decorator should be applied AFTER @require_git since it
        depends on the repository being available.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Get ctx from first positional arg (from @click.pass_obj)
        ctx_maybe: Any = args[0] if args else kwargs.get("ctx")

        if ctx_maybe is None:
            raise click.ClickException(
                "Internal error: RoarContext not available. "
                "Ensure @click.pass_obj is applied before @require_clean_git."
            )
        ctx: RoarContext = ctx_maybe

        if ctx.repo_root is None:
            # require_git should have caught this, but be defensive
            raise click.ClickException("Not in a git repository.")

        # Get VCS provider and check status
        try:
            from ..core.container import get_container

            container = get_container()
            vcs = container.get_vcs_provider("git")
            clean, changes = vcs.get_status(str(ctx.repo_root))

            if not clean:
                # Format error message with changed files
                lines = ["Git repository has uncommitted changes:"]
                for change in changes[:5]:
                    lines.append(f"  {change}")
                if len(changes) > 5:
                    lines.append(f"  ... and {len(changes) - 5} more")
                lines.append("")
                lines.append("Commit your changes before running this command.")

                raise click.ClickException("\n".join(lines))

        except click.ClickException:
            raise
        except Exception as e:
            raise click.ClickException(f"Failed to check git status: {e}") from e

        return f(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def pass_roar_context(f: F) -> F:
    """Convenience decorator combining @click.pass_obj with type hints.

    This is a thin wrapper around @click.pass_obj that provides better
    IDE support for the RoarContext type.

    Usage:
        @cli.command()
        @pass_roar_context
        def status(ctx: RoarContext):
            ...
    """
    return click.pass_obj(f)  # type: ignore[return-value]
