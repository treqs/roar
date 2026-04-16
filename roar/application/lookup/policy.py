"""Shared policy helpers for remote artifact fallback."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from ...integrations.config import config_get
from .models import ParsedRef


class ArtifactRemoteLookupOperation(str, Enum):
    """Query operations that may opt into remote artifact fallback."""

    SHOW = "show"
    DIFF = "diff"
    LABEL_SHOW = "label_show"
    REPRODUCE = "reproduce"


def remote_artifact_fallback_enabled(
    operation: ArtifactRemoteLookupOperation,
    parsed_ref: ParsedRef,
    *,
    start_dir: Path | None = None,
) -> bool:
    """Return whether remote artifact fallback should be attempted."""
    if not parsed_ref.is_artifact_lookup_candidate:
        return False

    if operation == ArtifactRemoteLookupOperation.REPRODUCE:
        return True

    start_dir_str = str(start_dir) if start_dir is not None else None
    return bool(config_get("glaas.query_nonlocal_ids_on_glaas", start_dir=start_dir_str))
