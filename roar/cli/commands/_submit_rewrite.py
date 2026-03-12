"""Compatibility shim over the canonical submit rewrite hook."""

from roar.execution.framework.contract import SubmitCommandRewrite
from roar.execution.framework.submit import SubmitBackendAdapter, maybe_rewrite_submit_command

__all__ = [
    "SubmitBackendAdapter",
    "SubmitCommandRewrite",
    "maybe_rewrite_submit_command",
]
