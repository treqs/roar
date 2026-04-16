"""Shared local-first / remote-second lookup runner."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from .models import LookupResult, LookupSource

T = TypeVar("T")


def run_local_then_remote_lookup(
    *,
    lookup_local: Callable[[], T | None],
    lookup_remote: Callable[[], tuple[T | None, str | None]],
    allow_remote: bool,
) -> LookupResult[T]:
    """Run a local lookup first, then an optional remote lookup on miss."""
    local_value = lookup_local()
    if local_value is not None:
        return LookupResult(value=local_value, source=LookupSource.LOCAL)

    if not allow_remote:
        return LookupResult(source=LookupSource.NONE)

    remote_value, remote_error = lookup_remote()
    if remote_error:
        return LookupResult(error=remote_error, source=LookupSource.REMOTE)
    if remote_value is not None:
        return LookupResult(value=remote_value, source=LookupSource.REMOTE)
    return LookupResult(source=LookupSource.NONE)
