"""Shared CLI formatting helpers — brand banner + hint block conventions.

Two surfaces share these primitives:

  * Brand banner: `🦖 roar <subcommand>` in `status_green`. First line
    of long-form commands so the surface always identifies itself.
  * Hints: amber `hint: …` lines, same convention git uses for its
    `advice.*` family.

Both go to **stderr** and are gated only by `hints.enabled` (config,
default ``True``) and `output.verbosity == "quiet"`. Putting them on
stderr keeps stdout machine-clean for pipes / redirects / agent
capture, while still surfacing advisory output to interactive users
*and* to non-TTY consumers (CI logs, agents like Claude Code) that
benefit from the nudges. ANSI color/emoji styling further gates on
``sys.stderr.isatty()`` so captured logs stay plain.

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

    Goes to stderr; suppressed by the same gate hints use
    (``hints.enabled = false`` or quiet verbosity). The roar version is
    available via ``roar --version`` rather than repeated on every
    invocation.
    """
    if not hints_should_print():
        return
    caps = detect(sys.stderr)
    prefix = "🦖" if caps.can_emoji else "roar:"
    click.echo(
        style(f"{prefix} roar {subcommand}", "status_green", enabled=caps.can_color),
        err=True,
    )


def hints_should_print() -> bool:
    """Whether to emit advisory ``hint:`` lines for this invocation.

    Suppressed when either holds:

    * ``hints.enabled = false`` in roar config (explicit opt-out).
    * ``output.verbosity = "quiet"`` (the ``-q`` analogue).

    Note: no TTY check. Hints go to stderr — stdout consumers stay
    clean by virtue of stream, not by the gate suppressing the line.
    Agents and CI logs that capture stderr see the nudges.
    """
    from ..integrations.config import config_get

    if config_get("hints.enabled") is False:
        return False
    return config_get("output.verbosity") != "quiet"


def make_hint_printer() -> tuple[TerminalCaps, Callable[..., None]]:
    """Return ``(caps, hint_fn)``. The closure captures caps so callers
    don't re-detect for every line. Caps come from ``sys.stderr`` (where
    hints go), so ANSI color/emoji only fire when stderr is a TTY."""
    caps = detect(sys.stderr)

    def hint(text: str = "") -> None:
        line = f"hint: {text}" if text else "hint:"
        click.echo(style(line, "warn_amber", enabled=caps.can_color), err=True)

    return caps, hint
