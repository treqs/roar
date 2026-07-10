"""Remote artifact lookup helpers shared by read-oriented commands."""

from __future__ import annotations

from typing import Any, Protocol

from ...core.exceptions import GlaasApiError
from ...integrations.glaas import GlaasClient


class SupportsArtifactLookup(Protocol):
    """Minimal read-only interface for remote artifact resolution."""

    def get_artifact(self, hash_prefix: str) -> dict[str, Any]: ...


def lookup_remote_artifact(
    *,
    hash_prefix: str,
    artifact_reader: SupportsArtifactLookup | None = None,
    server_url: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a remote artifact by hash prefix.

    Tries the authenticated lookup first. If that's rejected for lack of
    credentials (401/403) — the common case for a user with no SSH key
    registered looking up a public artifact — falls back to the public,
    optionally-authenticated endpoint so anonymous reads still work.

    Returns ``(artifact, error_message)``. A 404 is treated as a clean miss.
    """
    reader = artifact_reader or GlaasClient(base_url=server_url)

    try:
        artifact = reader.get_artifact(hash_prefix)
    except GlaasApiError as exc:
        if exc.status_code == 404:
            return None, None
        if exc.status_code in (401, 403) and hasattr(reader, "get_public_artifact"):
            return _lookup_public_artifact(reader, hash_prefix)
        return None, str(exc)
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)

    if not isinstance(artifact, dict):
        return None, None
    return artifact, None


def _lookup_public_artifact(
    reader: SupportsArtifactLookup, hash_prefix: str
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        artifact = reader.get_public_artifact(hash_prefix)  # type: ignore[attr-defined]
    except GlaasApiError as exc:
        if exc.status_code == 404:
            return None, None
        return None, str(exc)
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)

    if not isinstance(artifact, dict):
        return None, None
    return artifact, None
