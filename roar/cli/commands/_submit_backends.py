"""Compatibility shim over the shared execution-backend registry."""

from __future__ import annotations

from roar.services.execution.distributed_backends import iter_execution_backends

from ._submit_rewrite import SubmitBackendAdapter


def register_submit_backend(adapter: SubmitBackendAdapter) -> None:
    raise RuntimeError(
        "submit-specific backend registration has been replaced by roar.execution_backends"
    )


def iter_submit_backends() -> tuple[SubmitBackendAdapter, ...]:
    return tuple(
        SubmitBackendAdapter(
            name=backend.name,
            matches_command=backend.matches_submit_command,
            rewrite_command=backend.rewrite_submit_command,
        )
        for backend in iter_execution_backends()
    )
