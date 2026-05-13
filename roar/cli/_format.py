"""Shared CLI formatting helpers — brand banner + hint block conventions.

Two surfaces share these primitives:

  * Brand banner: `🦖 roar <subcommand>` in `status_green`. First line
    of long-form commands so the surface always identifies itself.
    Suppressed in non-TTY / quiet contexts via the same gate hints use,
    so machine consumers see clean output.
  * Hints: amber `hint: …` lines suppressed in non-interactive contexts
    (config knob, quiet verbosity, non-TTY stdout). Same convention git
    uses for its `advice.*` family.

Putting these in one place keeps `roar init`, `roar tracer`, and any
future command consistent. Each call site decides *what* to say; how
it's rendered is centralized here.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import click

from ..presenters.terminal import TerminalCaps, detect, style


def print_brand_header(subcommand: str) -> None:
    """First line of long-form output: `🦖 roar <subcommand>` in green.

    Shares `hints_should_print()`'s gate — suppressed in CI, redirected
    pipes, quiet mode, or when `hints.enabled = false`. The version is
    available via `roar --version` rather than repeated on every
    invocation.
    """
    if not hints_should_print():
        return
    caps = detect(sys.stdout)
    prefix = "🦖" if caps.can_emoji else "roar:"
    click.echo(style(f"{prefix} roar {subcommand}", "status_green", enabled=caps.can_color))


def hints_should_print() -> bool:
    """Whether to emit advisory `hint:` lines for this invocation.

    Suppressed when any of these holds:
      * `hints.enabled = false` in roar config.
      * `output.verbosity = "quiet"` (the `-q` analogue).
      * stdout isn't a TTY (CI, redirected, piped — same convention as
        the rest of roar).
    """
    from ..integrations.config import config_get

    if config_get("hints.enabled") is False:
        return False
    if config_get("output.verbosity") == "quiet":
        return False
    return sys.stdout.isatty()


def make_hint_printer() -> tuple[TerminalCaps, Callable[..., None]]:
    """Return (caps, hint_fn). The closure captures caps so callers don't
    re-detect for every line."""
    caps = detect(sys.stdout)

    def hint(text: str = "") -> None:
        line = f"hint: {text}" if text else "hint:"
        click.echo(style(line, "warn_amber", enabled=caps.can_color))

    return caps, hint
