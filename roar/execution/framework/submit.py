"""Canonical submit rewrite surface for distributed execution backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from roar.execution.framework.contract import SubmitCommandRewrite
from roar.services.execution.fragment_reconstitution import build_submit_finalizer

SubmitCommandMatcher = Callable[[list[str]], bool]
SubmitCommandRewriter = Callable[[list[str]], SubmitCommandRewrite]


@dataclass(frozen=True)
class SubmitBackendAdapter:
    name: str
    matches_command: SubmitCommandMatcher
    rewrite_command: SubmitCommandRewriter


def maybe_rewrite_submit_command(command: list[str]) -> SubmitCommandRewrite:
    from roar.execution.framework.registry import iter_execution_backends

    for backend in iter_execution_backends():
        if not backend.matches_submit_command(command):
            continue
        rewritten = backend.rewrite_submit_command(command)
        if (
            rewritten.finalize_run is None
            and rewritten.session_id
            and backend.fragment_reconstitution is not None
        ):
            rewritten = SubmitCommandRewrite(
                command=list(rewritten.command),
                session_id=rewritten.session_id,
                finalize_run=build_submit_finalizer(backend.name, rewritten.session_id),
            )
        return rewritten
    return SubmitCommandRewrite(command=command)


__all__ = [
    "SubmitBackendAdapter",
    "SubmitCommandRewrite",
    "maybe_rewrite_submit_command",
]
