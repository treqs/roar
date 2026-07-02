"""
Application-layer tag service for hereditary compliance tags.

Tags live under the ``tag.*`` namespace inside the existing versioned label
documents.  Each kind stores a JSON array (set semantics — no duplicate values).

  tag.license          → ["MIT", "Apache-2.0"]
  tag.contains_pii     → ["present"]
  tag.jurisdiction     → ["EU", "US"]
  tag.classification   → ["internal"]
  tag.special_category → ["biometric"]

TagService wraps LabelService so the CLI never touches the label repo directly.
"""

from __future__ import annotations

from typing import Any

from ..core.label_constants import TAG_NAMESPACE
from ..db.context import DatabaseContext
from .label_rendering import flatten_label_metadata
from .labels import LabelService, LabelTargetRef


class TagService:
    """Set-accumulation semantics over the tag.* label namespace."""

    def __init__(self, db_ctx: DatabaseContext, cwd: Any) -> None:
        self._svc = LabelService(db_ctx, cwd)

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def resolve_target(self, ref: str) -> LabelTargetRef:
        """Auto-detect entity type from the reference format.

        @N          → job step N in the active session
        <hex>       → artifact by hash prefix
        @BN         → raises ValueError (unsupported in P1)
        @session/@latest → raises ValueError (unsupported in P1)
        """
        if ref.startswith("@"):
            inner = ref[1:]
            if inner.upper().startswith("B"):
                raise ValueError(
                    "Build-step targets (@BN) are not yet supported by 'roar tag'. "
                    "Use the job UID directly instead."
                )
            if inner.lower() in ("session", "latest"):
                raise ValueError(
                    "Session targets (@session / @latest) are not yet supported by 'roar tag'."
                )
            return self._svc.resolve_target("job", ref)

        return self._svc.resolve_target("artifact", ref)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(self, resolved: LabelTargetRef, kind: str, value: str) -> bool:
        """Append *value* to the ``tag.{kind}`` set.

        Returns True when the document was actually changed (value was absent).
        Idempotent — adding a value already present is a no-op.
        """
        current = self._svc.current_metadata(resolved)
        tag_ns: dict[str, Any] = current.get(TAG_NAMESPACE) or {}
        existing = tag_ns.get(kind)

        if isinstance(existing, list):
            values: list[str] = existing
        elif existing is None:
            values = []
        else:
            # Scalar stored by a legacy path — promote to list.
            values = [str(existing)]

        if value in values:
            return False

        self._svc.set_metadata(resolved, {TAG_NAMESPACE: {kind: [*values, value]}})
        return True

    def remove(self, resolved: LabelTargetRef, kind: str, value: str | None) -> bool:
        """Remove *value* from ``tag.{kind}`` (or delete the entire kind if value is None).

        Returns True when the document changed.  No-ops silently return False.
        """
        current = self._svc.current_metadata(resolved)
        tag_ns: dict[str, Any] = current.get(TAG_NAMESPACE) or {}

        if kind not in tag_ns:
            return False

        if value is None:
            # Remove the whole kind key.
            result = self._svc.delete_keys(resolved, [f"{TAG_NAMESPACE}.{kind}"])
            return result.changed

        existing = tag_ns[kind]
        if isinstance(existing, list):
            values = existing
        else:
            values = [str(existing)]

        if value not in values:
            return False

        new_values = [v for v in values if v != value]
        if new_values:
            result = self._svc.set_metadata(resolved, {TAG_NAMESPACE: {kind: new_values}})
        else:
            result = self._svc.delete_keys(resolved, [f"{TAG_NAMESPACE}.{kind}"])
        return result.changed

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_tags(self, resolved: LabelTargetRef) -> dict[str, Any]:
        """Return only the ``tag.*`` subtree of the current label document."""
        current = self._svc.current_metadata(resolved)
        return current.get(TAG_NAMESPACE) or {}

    def history(self, resolved: LabelTargetRef) -> list[dict[str, Any]]:
        """Return full label-version history for the target."""
        return self._svc.history(resolved)


def flatten_tag_metadata(tags: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten the tag.* subtree into sorted ``(kind, display_value)`` pairs."""
    return flatten_label_metadata({TAG_NAMESPACE: tags}) if tags else []
