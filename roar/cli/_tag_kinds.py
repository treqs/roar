"""Validation for hereditary ``roar tag`` kinds.

The built-in canonical kinds are compliance-mapped and rendered by GLaaS. A
project extends the *allowed* set with its own hereditary kinds via
``[tags] custom_kinds`` in ``.roarconfig`` (committed, so a team shares it).

A non-allowed kind is **rejected** (not merely warned) — a typo like
``licence`` or ``contains_pil`` would otherwise propagate silently as a phantom
kind that never lands in the compliance report. The rejection prints a
copy-pasteable, append-aware hint that preserves any existing ``custom_kinds``.
"""

from __future__ import annotations

import click

from ..core.label_constants import CANONICAL_TAG_KINDS


def configured_custom_kinds(start_dir: str | None = None) -> list[str]:
    """Project-configured extra kinds from ``[tags] custom_kinds`` (deduped, ordered)."""
    from ..integrations.config import config_get

    raw = config_get("tags.custom_kinds", start_dir=start_dir)
    if not isinstance(raw, (list, tuple)):
        return []
    ordered: dict[str, None] = {}
    for item in raw:
        kind = str(item).strip()
        if kind:
            ordered.setdefault(kind, None)
    return list(ordered)


def allowed_tag_kinds(start_dir: str | None = None) -> frozenset[str]:
    """Canonical kinds plus any configured custom kinds."""
    return CANONICAL_TAG_KINDS | frozenset(configured_custom_kinds(start_dir))


def _hint_lines(kind: str, start_dir: str | None) -> list[str]:
    """A spelled-out, append-aware ``.roarconfig`` snippet that allows *kind*."""
    existing = configured_custom_kinds(start_dir)
    merged = existing if kind in existing else [*existing, kind]
    rendered = ", ".join(f'"{k}"' for k in merged)
    verb = "update" if existing else "add"
    return [
        f"to allow '{kind}', {verb} [tags] custom_kinds in .roarconfig:",
        "    [tags]",
        f"    custom_kinds = [{rendered}]",
    ]


def enforce_tag_kind(kind: str, *, start_dir: str | None = None) -> None:
    """Reject *kind* (with an actionable hint) unless it's allowed.

    A canonical kind, or one listed in ``[tags] custom_kinds``, passes silently.
    Otherwise prints ``Error:`` + a ``hint:`` snippet and exits non-zero.
    """
    if kind in allowed_tag_kinds(start_dir):
        return

    from ._format import hints_should_print, make_hint_printer

    click.echo(f"Error: '{kind}' is not a canonical tag kind.", err=True)
    if hints_should_print():
        _, hint = make_hint_printer()
        for line in _hint_lines(kind, start_dir):
            hint(line)
    raise SystemExit(1)
