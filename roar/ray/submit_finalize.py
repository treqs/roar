"""Compatibility wrapper for the shared submit finalizer."""

from roar.services.execution.fragment_reconstitution import build_submit_finalizer


def build_ray_submit_finalizer(session_id: str):
    return build_submit_finalizer("ray", session_id)
