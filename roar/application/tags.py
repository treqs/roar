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

from collections.abc import Iterable
from typing import Any, Protocol

from ..core.label_constants import TAG_NAMESPACE
from ..core.label_origins import LABEL_ORIGIN_SYSTEM
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
        values = _as_value_list(tag_ns.get(kind))

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

        values = _as_value_list(tag_ns[kind])

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


class _TagLabelRepo(Protocol):
    """Minimal label repo surface needed to propagate tags between artifacts."""

    def get_current(
        self, entity_type: str, *, artifact_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def create_version(
        self,
        entity_type: str,
        metadata: dict[str, Any],
        *,
        artifact_id: str | None = None,
        write_origin: str | None = None,
    ) -> dict[str, Any]: ...


def propagate_tags(
    label_repo: _TagLabelRepo,
    *,
    input_artifact_ids: Iterable[str],
    output_artifact_ids: Iterable[str],
    blocked_kinds: frozenset[str] = frozenset(),
) -> None:
    """Union ``tag.*`` values from a job's input artifacts onto its outputs.

    Every kind present on any input is merged (set semantics — no duplicate
    values) into every output's current tag namespace, except kinds listed in
    *blocked_kinds*. Writes are stamped with a system write-origin since the
    inheriting document is machine-derived, not user-asserted on that target.
    A no-op output write (nothing new to add) does not create a label version.
    """
    input_ids = list(dict.fromkeys(i for i in input_artifact_ids if i))
    output_ids = list(dict.fromkeys(o for o in output_artifact_ids if o))
    if not input_ids or not output_ids:
        return

    inherited: dict[str, list[str]] = {}
    for artifact_id in input_ids:
        tag_ns = _current_tag_namespace(label_repo, artifact_id)
        for kind, raw_values in tag_ns.items():
            if kind in blocked_kinds:
                continue
            bucket = inherited.setdefault(kind, [])
            for value in _as_value_list(raw_values):
                if value not in bucket:
                    bucket.append(value)

    if not inherited:
        return

    for artifact_id in output_ids:
        current = label_repo.get_current("artifact", artifact_id=artifact_id)
        current_metadata = current.get("metadata") if isinstance(current, dict) else {}
        if not isinstance(current_metadata, dict):
            current_metadata = {}

        existing_tag_ns = current_metadata.get(TAG_NAMESPACE)
        tag_ns = dict(existing_tag_ns) if isinstance(existing_tag_ns, dict) else {}
        changed = False
        for kind, incoming_values in inherited.items():
            existing_values = _as_value_list(tag_ns.get(kind))
            union = list(existing_values)
            for value in incoming_values:
                if value not in union:
                    union.append(value)
                    changed = True
            tag_ns[kind] = union

        if not changed:
            continue

        merged_metadata = dict(current_metadata)
        merged_metadata[TAG_NAMESPACE] = tag_ns
        label_repo.create_version(
            "artifact",
            merged_metadata,
            artifact_id=artifact_id,
            write_origin=LABEL_ORIGIN_SYSTEM,
        )


def _current_tag_namespace(label_repo: _TagLabelRepo, artifact_id: str) -> dict[str, Any]:
    current = label_repo.get_current("artifact", artifact_id=artifact_id)
    metadata = current.get("metadata") if isinstance(current, dict) else None
    tag_ns = metadata.get(TAG_NAMESPACE) if isinstance(metadata, dict) else None
    return tag_ns if isinstance(tag_ns, dict) else {}


def _as_value_list(existing: Any) -> list[str]:
    """Normalize a tag kind's stored value into a list (legacy scalars included)."""
    if isinstance(existing, list):
        return existing
    if existing is None:
        return []
    return [str(existing)]
