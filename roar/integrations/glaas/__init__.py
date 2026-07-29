"""Lazy exports for GLaaS integration helpers."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ArtifactRegistrationService": ".registration",
    "GlaasClient": ".client",
    "GlaasFragmentStreamer": ".fragment_streamer",
    "JobRegistrationService": ".registration",
    "RegistrationCoordinator": ".registration",
    "SessionRegistrationService": ".registration",
    "compute_pubkey_fingerprint": ".auth",
    "create_signature_payload": ".auth",
    "find_ssh_private_key": ".auth",
    "find_ssh_pubkey": ".auth",
    "get_glaas_url": ".auth",
    "make_auth_header": ".auth",
    "parse_json_response": ".transport",
    "renew_fragment_session": ".fragment_streamer",
    "request_json": ".transport",
    "sign_payload": ".auth",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
