"""
Application-layer tag service for hereditary compliance tags.

Tags live under the ``tag.*`` namespace inside the existing versioned label
documents.  Each kind stores a **list of provenance records** (set semantics
over each record's ``value`` — no duplicate values within a kind):

  tag.license          -> {"values": [{"value": "MIT", "origin": "user"},
                                       {"value": "Apache-2.0", "origin": "system", "job": "<uid>"}]}
  tag.contains_pii     -> {"values": [{"value": "present", "origin": "user"}]}

``origin`` is ``"user"`` (an explicit human act) or ``"system"`` (inherited
via propagation at job-record time). ``job`` is the producing job's UID —
present whenever a job was involved (system-derived values, and user-origin
values stamped via ``roar run --add-tag``); a bare CLI ``tag add`` has no job
and omits it. ``job`` is what the scope check (below) uses to resolve which
session a value belongs to.

**Scope and the bind ledger.** Tags propagate automatically and fully within
one session (over-approximate — false positives are contained and cheap).
Crossing a session boundary requires an explicit **bind**: a human act
naming the artifact whose tags are of record. Bound-ness is a ledger lookup,
not a flag on the value — ``tag.bind`` holds an append-only list of
bind/unbind events, each recording the ``(kind, value)`` pairs it covers:

  tag.bind -> {"events": [{"action": "bind", "covers": {"contains_pii": ["present"]}}]}

A user-origin ``tag add`` writes an implicit bind event for the value it
just added — "one mechanism, no special cases" (see ``TagService.add``).

TagService wraps the raw label repository directly (not ``LabelService``) so
its writes aren't blocked by the ``tag.*``/``attach.*`` reservation that
protects the generic ``roar label`` path — see ``system_labels.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.label_constants import TAG_NAMESPACE
from ..core.label_origins import LABEL_ORIGIN_SYSTEM, LABEL_ORIGIN_USER
from ..db.context import DatabaseContext
from .labels import LabelService, LabelTargetRef

# Reserved kind name within the tag.* namespace: the bind ledger itself, never
# a real hereditary kind. Excluded from get_tags()/propagation/covers-all.
BIND_KIND = "bind"


@dataclass(frozen=True)
class BindResult:
    """Outcome of a bind/unbind call — what the CLI echoes."""

    changed: bool
    promoted: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class WhyNode:
    """One hop in a ``roar tag why`` provenance walk (a render-agnostic tree).

    Each node's ``label`` describes one step back toward a human act; leaves are
    a user ``tag add`` / ``run --add-tag`` (or an unresolved origin).
    """

    label: str
    children: list[WhyNode] = field(default_factory=list)


class TagService:
    """Set-accumulation semantics over the tag.* label namespace."""

    def __init__(self, db_ctx: DatabaseContext, cwd: Any) -> None:
        self._svc = LabelService(db_ctx, cwd)
        self._label_repo = db_ctx.labels
        # Read-only handles used by `why` to walk job -> inputs and name artifacts.
        self._jobs = db_ctx.jobs
        self._artifacts = db_ctx.artifacts

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def resolve_target(self, ref: str) -> LabelTargetRef:
        """Auto-detect entity type from the reference format.

        @N          -> job step N in the active session
        <hex>       -> artifact by hash prefix
        @BN         -> raises ValueError (unsupported in P1)
        @session/@latest -> raises ValueError (unsupported in P1)
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

        Writes an implicit bind event covering exactly ``(kind, value)`` in the
        same version — a user-origin ``tag add`` is "born bound" (the
        named-artifact rule: this act names a concrete, inspectable artifact).
        """
        subtree = self._current_tag_subtree(resolved)
        records = _as_value_records(subtree.get(kind))
        if any(record["value"] == value for record in records):
            return False

        new_subtree = dict(subtree)
        new_subtree[kind] = {"values": [*records, {"value": value, "origin": LABEL_ORIGIN_USER}]}
        new_subtree[BIND_KIND] = _with_appended_bind_event(
            subtree.get(BIND_KIND), action="bind", covers={kind: [value]}
        )
        self._write_tag_subtree(resolved, new_subtree)
        return True

    def remove(self, resolved: LabelTargetRef, kind: str, value: str | None) -> bool:
        """Remove *value* from ``tag.{kind}`` (or delete the entire kind if value is None).

        Returns True when the document changed. No-ops silently return False.
        Does not touch the bind ledger — a removed value simply won't be
        present for future propagation; past bind events remain as history,
        matching the append-only, never-destructive record.
        """
        subtree = self._current_tag_subtree(resolved)
        if kind not in subtree:
            return False

        if value is None:
            new_subtree = {k: v for k, v in subtree.items() if k != kind}
            self._write_tag_subtree(resolved, new_subtree)
            return True

        records = _as_value_records(subtree.get(kind))
        if not any(record["value"] == value for record in records):
            return False

        remaining = [record for record in records if record["value"] != value]
        new_subtree = dict(subtree)
        if remaining:
            new_subtree[kind] = {"values": remaining}
        else:
            new_subtree.pop(kind, None)
        self._write_tag_subtree(resolved, new_subtree)
        return True

    def bind(self, resolved: LabelTargetRef) -> BindResult:
        """Promote every tag value currently on *resolved* to cross-session scope.

        Snapshot semantics: covers exactly the current `(kind, value)` set at
        bind time. Later within-session derivations don't ride an old bind.
        A repeat bind that covers exactly the same set as the most recent
        bind event is a no-op — it doesn't add a redundant ledger entry.
        """
        subtree = self._current_tag_subtree(resolved)
        covers = _covers_all_current_values(subtree)
        if not covers:
            return BindResult(changed=False)

        events = _as_bind_events(subtree.get(BIND_KIND))
        if events and events[-1].get("action") == "bind" and events[-1].get("covers") == covers:
            return BindResult(changed=False, promoted=covers)

        new_subtree = dict(subtree)
        new_subtree[BIND_KIND] = _with_appended_bind_event(
            subtree.get(BIND_KIND), action="bind", covers=covers
        )
        self._write_tag_subtree(resolved, new_subtree)
        return BindResult(changed=True, promoted=covers)

    def unbind(self, resolved: LabelTargetRef) -> BindResult:
        """Revoke every `(kind, value)` pair currently bound on *resolved*.

        One call heals the whole cone: everything that inherited through a
        revoked bind is mechanically identifiable (superseded), matching the
        append-only revocation model — this writes a new event, never deletes
        the bind it revokes.
        """
        subtree = self._current_tag_subtree(resolved)
        events = _as_bind_events(subtree.get(BIND_KIND))
        covers = _currently_bound_pairs(events)
        if not covers:
            return BindResult(changed=False)

        new_subtree = dict(subtree)
        new_subtree[BIND_KIND] = {"events": [*events, {"action": "unbind", "covers": covers}]}
        self._write_tag_subtree(resolved, new_subtree)
        return BindResult(changed=True, promoted=covers)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_tags(self, resolved: LabelTargetRef) -> dict[str, Any]:
        """Return only the ``tag.*`` subtree of the current label document, excluding the bind ledger."""
        subtree = self._current_tag_subtree(resolved)
        return {kind: value for kind, value in subtree.items() if kind != BIND_KIND}

    def history(self, resolved: LabelTargetRef) -> list[dict[str, Any]]:
        """Return full label-version history for the target."""
        return self._svc.history(resolved)

    def why(self, resolved: LabelTargetRef, kind: str, value: str | None = None) -> list[WhyNode]:
        """Explain how *resolved* acquired ``tag.{kind}`` — one tree per value.

        Read-only traversal over the stored ``{value, origin, job}`` records and
        the bind ledger (no writes, no schema). Each tree bottoms out at a human
        act (a bare ``tag add`` or a ``run --add-tag``), annotating any
        cross-session hop with the explicit ``bind`` that authorized it.
        """
        if resolved.entity_type == "job":
            raise ValueError(
                "`roar tag why` explains an artifact's tag, not a job's. `@N` targets "
                "job step N — trace one of its output artifacts instead (by hash or "
                "path), or use `roar tag show @N` to list the job's tags."
            )
        if resolved.entity_type != "artifact" or not resolved.artifact_id:
            raise ValueError(
                "`roar tag why` explains an artifact's tag — target a tracked artifact "
                "by hash or path."
            )
        subtree = self._current_tag_subtree(resolved)
        stored = [record["value"] for record in _as_value_records(subtree.get(kind))]
        wanted = [v for v in stored if value is None or v == value]
        return [self._explain(resolved.artifact_id, kind, v, frozenset()) for v in wanted]

    def _explain(self, artifact_id: str, kind: str, value: str, visited: frozenset[str]) -> WhyNode:
        name = self._artifact_display(artifact_id)
        subtree = _current_tag_subtree(self._label_repo, artifact_id)
        record = next(
            (r for r in _as_value_records(subtree.get(kind)) if r["value"] == value), None
        )
        if record is None:
            return WhyNode(f"{name}: no {kind}={value} recorded")

        origin = record.get("origin")
        job_uid = record.get("job")
        if origin == LABEL_ORIGIN_USER and not job_uid:
            return WhyNode(f"{name}: {kind}={value} — user `roar tag add` (born bound)")
        if origin == LABEL_ORIGIN_USER and job_uid:
            return WhyNode(
                f"{name}: {kind}={value} — user `roar run --add-tag` "
                f"in job {job_uid[:8]} (session-scoped)"
            )

        header = f"{name}: {kind}={value} — inherited"
        header += f" via job {job_uid[:8]}" if job_uid else ""
        if artifact_id in visited:
            return WhyNode(header + " (cycle)")
        if not job_uid:
            return WhyNode(header + " (no producing job recorded — origin unknown)")
        job = self._jobs.get_by_uid(job_uid)
        if not job:
            return WhyNode(header + " (producing job not found)")

        visited = visited | {artifact_id}
        children: list[WhyNode] = []
        for inp in self._jobs.get_inputs(job["id"]):
            input_id = inp.get("artifact_id")
            if not input_id:
                continue
            input_subtree = _current_tag_subtree(self._label_repo, input_id)
            input_record = next(
                (r for r in _as_value_records(input_subtree.get(kind)) if r["value"] == value),
                None,
            )
            if input_record is None:
                continue  # this input doesn't carry the value — not on the path
            node = self._explain(input_id, kind, value, visited)
            # A *derived* (system) value made cross-session by an explicit bind is a
            # human act worth naming; a born-bound user tag already reads as one.
            covered = _is_covered_by_bind(
                _as_bind_events(input_subtree.get(BIND_KIND)), kind, value
            )
            if covered and input_record.get("origin") == LABEL_ORIGIN_SYSTEM:
                node = WhyNode(
                    f"`roar tag bind` on {self._artifact_display(input_id)} "
                    f"authorized this across sessions",
                    [node],
                )
            children.append(node)

        if not children:
            return WhyNode(header + " (no input carries this value — origin unclear)")
        return WhyNode(header, children)

    def _artifact_display(self, artifact_id: str) -> str:
        artifact = self._artifacts.get(artifact_id)
        if not artifact:
            return artifact_id[:12]
        path = artifact.get("path") or artifact.get("first_seen_path")
        if path:
            return str(path).rsplit("/", 1)[-1]
        hashes = artifact.get("hashes") or []
        digest = hashes[0]["digest"] if hashes else artifact_id
        return str(digest)[:12]

    # ------------------------------------------------------------------
    # Internal read/write — bypasses LabelService.set_metadata so writes
    # aren't rejected by the tag.*/attach.* reservation (system_labels.py)
    # that protects the generic `roar label` path.
    # ------------------------------------------------------------------

    def _current_tag_subtree(self, resolved: LabelTargetRef) -> dict[str, Any]:
        current = self._label_repo.get_current(
            resolved.entity_type,
            session_id=resolved.session_id,
            job_id=resolved.job_id,
            artifact_id=resolved.artifact_id,
        )
        metadata = current.get("metadata") if isinstance(current, dict) else None
        return _tag_subtree_from_metadata(metadata)

    def _write_tag_subtree(self, resolved: LabelTargetRef, new_subtree: dict[str, Any]) -> None:
        current = self._label_repo.get_current(
            resolved.entity_type,
            session_id=resolved.session_id,
            job_id=resolved.job_id,
            artifact_id=resolved.artifact_id,
        )
        metadata = current.get("metadata") if isinstance(current, dict) else None
        full_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        if new_subtree:
            full_metadata[TAG_NAMESPACE] = new_subtree
        else:
            full_metadata.pop(TAG_NAMESPACE, None)
        self._label_repo.create_version(
            resolved.entity_type,
            full_metadata,
            session_id=resolved.session_id,
            job_id=resolved.job_id,
            artifact_id=resolved.artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )


def tag_display_values(kind_data: Any) -> list[str]:
    """Extract just the ``value`` strings from a stored tag kind's record list, for display.

    Drops provenance (``origin``/``job``) — callers that want that detail
    (a future `roar tag why`) should read the records directly via
    ``_as_value_records``.
    """
    return [record["value"] for record in _as_value_records(kind_data)]


def tag_display_pairs(tag_subtree: Any) -> list[tuple[str, str]]:
    """``(kind, "v1, v2")`` display pairs for a ``tag.*`` subtree.

    Sorted by kind, skips the internal ``bind`` ledger and empty kinds. The one
    shared source of truth for how tags render in both ``roar tag show`` and
    ``roar show`` — so the two can't drift.
    """
    pairs: list[tuple[str, str]] = []
    if not isinstance(tag_subtree, dict):
        return pairs
    for kind in sorted(tag_subtree):
        if kind == BIND_KIND:
            continue
        values = tag_display_values(tag_subtree[kind])
        if values:
            pairs.append((kind, ", ".join(values)))
    return pairs


def barrier_items(run_modifiers: Any) -> list[str]:
    """The ``--block-tag`` items (barriers) recorded on a job, for display.

    Reads the ``run_modifiers.block_tags`` metadata; empty/absent yields ``[]``.
    """
    if not isinstance(run_modifiers, dict):
        return []
    return [str(item) for item in (run_modifiers.get("block_tags") or []) if str(item).strip()]


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
    current_session_id: int | None,
    resolve_job_session_id: Callable[[str], int | None],
    job_uid: str | None = None,
    blocked_kinds: frozenset[str] = frozenset(),
    blocked_values: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Union ``tag.*`` values from a job's input artifacts onto its outputs.

    Every kind present on any input is merged (set semantics — no duplicate
    values) into every output's current tag namespace, except kinds listed in
    *blocked_kinds* (whole-kind barriers, ``--block-tag KIND``) and individual
    ``(kind, value)`` pairs in *blocked_values* (value barriers,
    ``--block-tag KIND=VALUE`` — e.g. filtering ``license=GPL-3.0`` off a
    relicensing step's outputs while keeping the rest of the set). Writes are
    stamped with a system write-origin since the inheriting document is
    machine-derived, not user-asserted on that target. A no-op output write
    (nothing new to add) does not create a label version.

    **Scope-gated**: a candidate value only joins the union if it's in scope
    for *this* session — either it was produced by a job in the current
    session (resolved via *resolve_job_session_id*), or it's covered by a
    bind on the input artifact (see ``TagService.bind``). This is what keeps
    tags from riding bare content-hash identity across unrelated sessions
    (e.g. every 0-byte file in existence sharing one hash).
    """
    input_ids = list(dict.fromkeys(i for i in input_artifact_ids if i))
    output_ids = list(dict.fromkeys(o for o in output_artifact_ids if o))
    if not input_ids or not output_ids:
        return

    session_cache: dict[str, int | None] = {}

    def _job_session(job: str) -> int | None:
        if job not in session_cache:
            session_cache[job] = resolve_job_session_id(job)
        return session_cache[job]

    inherited: dict[str, list[str]] = {}
    for artifact_id in input_ids:
        subtree = _current_tag_subtree(label_repo, artifact_id)
        bind_events = _as_bind_events(subtree.get(BIND_KIND))
        for kind, kind_data in subtree.items():
            if kind == BIND_KIND or kind in blocked_kinds:
                continue
            blocked_vals = blocked_values.get(kind) if blocked_values else None
            bucket = inherited.setdefault(kind, [])
            for record in _as_value_records(kind_data):
                value = record["value"]
                if value in bucket:
                    continue
                if blocked_vals and value in blocked_vals:
                    continue  # value barrier: --block-tag KIND=VALUE filters this one value
                if _value_in_scope(record, current_session_id, _job_session) or _is_covered_by_bind(
                    bind_events, kind, value
                ):
                    bucket.append(value)

    inherited = {kind: values for kind, values in inherited.items() if values}
    if not inherited:
        return

    for artifact_id in output_ids:
        _merge_tags_into_artifact(
            label_repo, artifact_id, inherited, LABEL_ORIGIN_SYSTEM, job_uid=job_uid
        )


def stamp_tags(
    label_repo: _TagLabelRepo,
    *,
    output_artifact_ids: Iterable[str],
    tags: dict[str, list[str]],
    job_uid: str | None = None,
) -> None:
    """Stamp explicit ``KIND=VALUE`` tags directly onto a job's output artifacts.

    Unlike ``propagate_tags`` (inherited from inputs), these values were
    explicitly requested by the user at record time (e.g. via
    ``roar run --add-tag license=MIT``), so writes use the user write-origin
    — the same origin as a manual ``roar tag add``. Per the named-artifact
    rule, this does **not** imply a bind: it quantifies over the job's whole
    output set sight-unseen, so the values stay session-scoped (via the
    stamped ``job_uid``) until a named artifact is explicitly bound.
    """
    if not tags:
        return
    output_ids = list(dict.fromkeys(o for o in output_artifact_ids if o))
    if not output_ids:
        return

    for artifact_id in output_ids:
        _merge_tags_into_artifact(label_repo, artifact_id, tags, LABEL_ORIGIN_USER, job_uid=job_uid)


def parse_tag_kv(kv: str) -> tuple[str, str]:
    """Parse ``kind=value``. Raises ValueError on bad format."""
    if "=" not in kv:
        raise ValueError(f"Expected KIND=VALUE (e.g. license=MIT), got: {kv!r}")
    kind, _, value = kv.partition("=")
    kind = kind.strip()
    value = value.strip()
    if not kind:
        raise ValueError(f"Kind cannot be empty in: {kv!r}")
    if not value:
        raise ValueError(f"Value cannot be empty in: {kv!r}")
    return kind, value


def parse_block_tags(pairs: Iterable[str]) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Split ``--block-tag`` items into whole-kind and per-value barriers.

    ``KIND`` blocks the whole kind; ``KIND=VALUE`` filters a single value from
    the inherited set. Returns ``(blocked_kinds, blocked_values)``. A whole-kind
    block wins over a value-level one for the same kind (the kind is dropped
    entirely, so its value-level entries are irrelevant and omitted). ``KIND=``
    with an empty value is treated as a whole-kind block.
    """
    whole: set[str] = set()
    values: dict[str, set[str]] = {}
    for pair in pairs:
        item = pair.strip()
        if not item:
            continue
        if "=" in item:
            kind, _, value = item.partition("=")
            kind, value = kind.strip(), value.strip()
            if kind and value:
                values.setdefault(kind, set()).add(value)
            elif kind:
                whole.add(kind)
        else:
            whole.add(item)
    blocked_values = {k: frozenset(v) for k, v in values.items() if k not in whole}
    return frozenset(whole), blocked_values


def parse_add_tags(pairs: Iterable[str]) -> dict[str, list[str]]:
    """Parse repeated ``KIND=VALUE`` strings into a grouped, deduped tag dict."""
    grouped: dict[str, list[str]] = {}
    for pair in pairs:
        kind, value = parse_tag_kv(pair)
        bucket = grouped.setdefault(kind, [])
        if value not in bucket:
            bucket.append(value)
    return grouped


# ---------------------------------------------------------------------------
# Shared value/ledger primitives
# ---------------------------------------------------------------------------


def _tag_subtree_from_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    subtree = metadata.get(TAG_NAMESPACE)
    return subtree if isinstance(subtree, dict) else {}


def _as_value_records(existing: Any) -> list[dict[str, Any]]:
    """Normalize a tag kind's stored value into its list of provenance records."""
    if isinstance(existing, dict) and isinstance(existing.get("values"), list):
        return [
            record
            for record in existing["values"]
            if isinstance(record, dict) and "value" in record
        ]
    return []


def _as_bind_events(bind_doc: Any) -> list[dict[str, Any]]:
    if isinstance(bind_doc, dict) and isinstance(bind_doc.get("events"), list):
        return [event for event in bind_doc["events"] if isinstance(event, dict)]
    return []


def _with_appended_bind_event(
    bind_doc: Any, *, action: str, covers: dict[str, list[str]]
) -> dict[str, Any]:
    events = _as_bind_events(bind_doc)
    return {"events": [*events, {"action": action, "covers": covers}]}


def _is_covered_by_bind(events: list[dict[str, Any]], kind: str, value: str) -> bool:
    """Newest event covering (kind, value) wins; True iff that event is a bind."""
    for event in reversed(events):
        covers = event.get("covers") or {}
        if value in (covers.get(kind) or []):
            return event.get("action") == "bind"
    return False


def _covers_all_current_values(subtree: dict[str, Any]) -> dict[str, list[str]]:
    """Every `(kind, value)` pair currently stored on the subtree (excluding the bind ledger itself)."""
    covers: dict[str, list[str]] = {}
    for kind, kind_data in subtree.items():
        if kind == BIND_KIND:
            continue
        values = [record["value"] for record in _as_value_records(kind_data)]
        if values:
            covers[kind] = values
    return covers


def _currently_bound_pairs(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Every `(kind, value)` pair ever mentioned whose newest covering event is a bind."""
    all_pairs: set[tuple[str, str]] = set()
    for event in events:
        covers = event.get("covers") or {}
        for kind, values in covers.items():
            for value in values:
                all_pairs.add((kind, value))

    bound: dict[str, list[str]] = {}
    for kind, value in sorted(all_pairs):
        if _is_covered_by_bind(events, kind, value):
            bound.setdefault(kind, []).append(value)
    return bound


def _value_in_scope(
    record: dict[str, Any],
    current_session_id: int | None,
    job_session: Callable[[str], int | None],
) -> bool:
    """A record with no job pointer (a bare user tag) always needs a bind.

    Otherwise, in-scope iff the producing job's session matches the current
    one — including both being unassigned (``None``), which keeps
    session-less job recording (``assign_to_session=False``, used outside
    normal pipeline runs) working the same as before scope-gating existed.
    """
    job = record.get("job")
    if not isinstance(job, str) or not job:
        return False
    return job_session(job) == current_session_id


def _current_tag_subtree(label_repo: _TagLabelRepo, artifact_id: str) -> dict[str, Any]:
    current = label_repo.get_current("artifact", artifact_id=artifact_id)
    metadata = current.get("metadata") if isinstance(current, dict) else None
    return _tag_subtree_from_metadata(metadata)


def _merge_tags_into_artifact(
    label_repo: _TagLabelRepo,
    artifact_id: str,
    incoming: dict[str, list[str]],
    write_origin: str,
    *,
    job_uid: str | None = None,
) -> None:
    """Union *incoming* kind/value pairs into one artifact's current tag namespace."""
    tag_subtree = dict(_current_tag_subtree(label_repo, artifact_id))
    current = label_repo.get_current("artifact", artifact_id=artifact_id)
    current_metadata = current.get("metadata") if isinstance(current, dict) else {}
    if not isinstance(current_metadata, dict):
        current_metadata = {}

    changed = False
    for kind, incoming_values in incoming.items():
        existing_records = _as_value_records(tag_subtree.get(kind))
        existing_values = {record["value"] for record in existing_records}
        new_records = list(existing_records)
        for value in incoming_values:
            if value in existing_values:
                continue
            record: dict[str, Any] = {"value": value, "origin": write_origin}
            if job_uid:
                record["job"] = job_uid
            new_records.append(record)
            existing_values.add(value)
            changed = True
        tag_subtree[kind] = {"values": new_records}

    if not changed:
        return

    merged_metadata = dict(current_metadata)
    merged_metadata[TAG_NAMESPACE] = tag_subtree
    label_repo.create_version(
        "artifact",
        merged_metadata,
        artifact_id=artifact_id,
        write_origin=write_origin,
    )
