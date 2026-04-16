"""Shared local/remote lookup helpers for query-style workflows."""

from .models import LookupResult, LookupSource, ParsedRef, RefKind
from .policy import ArtifactRemoteLookupOperation, remote_artifact_fallback_enabled
from .refs import classify_ref, parse_ref
from .remote_artifacts import SupportsArtifactLookup, lookup_remote_artifact
from .runner import run_local_then_remote_lookup

__all__ = [
    "ArtifactRemoteLookupOperation",
    "LookupResult",
    "LookupSource",
    "ParsedRef",
    "RefKind",
    "SupportsArtifactLookup",
    "classify_ref",
    "lookup_remote_artifact",
    "parse_ref",
    "remote_artifact_fallback_enabled",
    "run_local_then_remote_lookup",
]
