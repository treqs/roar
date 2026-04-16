"""Shared reference parsing helpers for query commands."""

from __future__ import annotations

from .models import ParsedRef, RefKind


def classify_ref(ref: str) -> RefKind:
    """Classify a raw CLI ref into a normalized kind."""
    if ref.startswith("@"):
        return RefKind.JOB_STEP
    if "/" in ref or ref.startswith(("./", "../", "~")):
        return RefKind.FILE_PATH

    is_hex = bool(ref) and all(char in "0123456789abcdefABCDEF" for char in ref)
    if is_hex and len(ref) <= 8:
        return RefKind.JOB_UID
    if is_hex and len(ref) > 8:
        return RefKind.ARTIFACT_HASH
    return RefKind.PATH_CANDIDATE


def parse_ref(ref: str, *, selector: str = "auto") -> ParsedRef:
    """Parse a raw ref with an optional explicit selector."""
    normalized_selector = selector or "auto"

    if normalized_selector == "session":
        kind = RefKind.SESSION
    elif normalized_selector == "path":
        kind = RefKind.FILE_PATH
    elif normalized_selector == "artifact":
        kind = RefKind.ARTIFACT_HASH
    elif normalized_selector == "job":
        kind = RefKind.JOB_STEP if ref.startswith("@") else RefKind.JOB_UID
    else:
        kind = classify_ref(ref)

    return ParsedRef(raw=ref, kind=kind, selector=normalized_selector)
