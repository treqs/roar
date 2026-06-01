"""Typed result DTOs for publish workflows."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegisterTagSummary:
    """Outcome of the roar-tag push that precedes a GLaaS register.

    Populated by `ensure_roar_tags_pushed`. The CLI renders this so the
    user can see exactly which commits were tagged and where they were
    pushed (or why the push was skipped).
    """

    session_tag: str | None = None
    job_tags: tuple[str, ...] = ()
    remote: str | None = None
    push_skipped_reason: str | None = None


@dataclass(frozen=True)
class RegisterLineageResponse:
    """Application response for `roar register`."""

    success: bool
    session_hash: str = ""
    session_url: str | None = None
    artifact_hash: str = ""
    jobs_registered: int = 0
    artifacts_registered: int = 0
    links_created: int = 0
    error: str | None = None
    secrets_detected: list[str] = field(default_factory=list)
    secrets_redacted: bool = False
    aborted_by_user: bool = False
    tag_summary: RegisterTagSummary | None = None
    warnings: list[str] = field(default_factory=list)
    # False when the registered lineage has no git commit (run captured
    # outside a git repository). GLaaS records it with ``pin_mode: missing``;
    # the CLI surfaces a notice so the user knows it can't be reproduced from
    # source. Defaults True so existing/normal registrations are unaffected.
    reproducible: bool = True


@dataclass(frozen=True)
class PutUploadedFile:
    local_path: str
    remote_url: str
    artifact_id: str | None = None
    hash: str | None = None


@dataclass(frozen=True)
class PutDryRunItem:
    path: str
    exists: bool


@dataclass(frozen=True)
class PutCompositeRegistration:
    root_path: str | None = None
    hash: str | None = None
    component_count_stored: int | None = None
    component_count_total: int | None = None
    artifact_id: str | None = None
    registered: bool | None = None
    local_persisted: bool | None = None
    local_error: str | None = None


@dataclass(frozen=True)
class PutResponse:
    """Application response for `roar put`."""

    success: bool
    destination: str
    job_id: int | None = None
    job_uid: str | None = None
    session_hash: str | None = None
    session_url: str | None = None
    dry_run: bool = False
    uploaded_files: list[PutUploadedFile] = field(default_factory=list)
    would_upload: list[PutDryRunItem] = field(default_factory=list)
    composites_registered: list[PutCompositeRegistration] = field(default_factory=list)
    git_tag: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
