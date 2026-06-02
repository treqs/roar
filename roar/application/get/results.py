"""Typed result DTOs for `roar get` application flows."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GetDownloadedFile:
    remote_url: str
    local_path: str
    hash: str | None = None
    size: int | None = None
    remote_key: str | None = None  # full manifest path (joins to the LFS sha256 oid)


@dataclass(frozen=True)
class GetDryRunItem:
    remote_url: str
    local_path: str


@dataclass(frozen=True)
class GetResponse:
    """Application response for a get workflow."""

    success: bool
    source: str
    job_id: int | None = None
    job_uid: str | None = None
    downloaded_files: list[GetDownloadedFile] = field(default_factory=list)
    dry_run: bool = False
    would_download: list[GetDryRunItem] = field(default_factory=list)
    git_tag: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
