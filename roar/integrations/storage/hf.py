"""Hugging Face Hub storage backend (``roar put``).

Uploads artifacts to a HF repo (model | dataset | space) and returns the public
``resolve`` URL, so a reproduced artifact gets a canonical, stranger-fetchable
``downloadLocation`` — the output-side of the lineage story ([71]).

Uses ``huggingface_hub`` via a lazy import (mirroring the GCS backend): the
dependency is only needed when someone actually runs ``roar put hf://...``.
(The hf *download* backend talks to the HF HTTP API directly with urllib, since
reads are simple; uploads use the LFS/commit protocol, so the library earns its
keep here.) The ``hf://`` URL convention matches the download side.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import StorageBackend

# Lazy import to avoid ImportError when huggingface_hub isn't installed.
HfApi = None


def _ensure_hf() -> None:
    """Ensure huggingface_hub is available, raising ImportError if not."""
    global HfApi
    if HfApi is None:
        from huggingface_hub import HfApi as _HfApi

        HfApi = _HfApi


_HF = "https://huggingface.co"
# URL path prefix -> HfApi repo_type (singular)
_PREFIXES = {"datasets": "dataset", "models": "model", "spaces": "space"}


def parse_hf_destination(bucket: str, prefix: str) -> tuple[str, str, str]:
    """Return ``(repo_type, repo_id, path_prefix)`` for a parsed ``hf://`` dest.

    ``hf://<owner>/<name>[/sub]``                 -> model  (put publishes models
                                                    by default)
    ``hf://datasets|models|spaces/<owner>/<name>[/sub]``

    (Note: unlike the *download* side, which defaults to ``datasets``, ``put``
    defaults to ``model`` — the campaign publishes reproduced models, and a
    dataset is the explicit, license-gated exception via the ``datasets/`` prefix.)
    """
    parts = [p for p in f"{bucket}/{prefix}".split("/") if p]
    repo_type = "model"
    if parts and parts[0] in _PREFIXES:
        repo_type = _PREFIXES[parts[0]]
        parts = parts[1:]
    if len(parts) < 2:
        raise ValueError(
            "hf:// destination must be hf://<owner>/<name>[/path] "
            "(or hf://datasets/<owner>/<name>[/path]); got "
            f"hf://{bucket}/{prefix}".rstrip("/")
        )
    repo_id = f"{parts[0]}/{parts[1]}"
    return repo_type, repo_id, "/".join(parts[2:])


def _token() -> str | None:
    """HF token from the usual env vars, else ``~/.hf_token`` (matches download)."""
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    path = Path("~/.hf_token").expanduser()
    return path.read_text().strip() if path.exists() else None


class HFBackend(StorageBackend):
    """Upload artifacts to a **pre-existing** Hugging Face Hub repo.

    Auth: a write-scoped HF token from ``HF_TOKEN`` (or ``~/.hf_token``).

    roar does **not** create the repo, and makes no decision about its
    visibility — exactly as the S3/GCS backends write a key into a bucket they
    never create. The repo's existence and its public/private setting are the
    operator's, decided once on HF; ``roar put`` only writes files into it. If
    the repo doesn't exist, ``upload`` fails with an actionable error rather
    than silently creating one (which would have to guess a visibility).

    ``upload`` returns the public ``resolve`` URL, which roar records as the
    artifact's ``source_url``/``downloadLocation``.
    """

    def __init__(self, bucket: str, prefix: str = ""):
        _ensure_hf()
        assert HfApi is not None  # guaranteed by _ensure_hf()
        self._repo_type, self._repo_id, self._prefix = parse_hf_destination(bucket, prefix)
        self._token = _token()
        self._api = HfApi(token=self._token)
        self._repo_checked = False

    def _ensure_repo_exists(self) -> None:
        """Fail fast with a clear message if the target repo doesn't exist.

        Mirrors S3/GCS behaviour on a missing bucket: roar never creates the
        container, so a missing repo is the operator's to create (with their
        intended visibility), not roar's to conjure."""
        if self._repo_checked:
            return
        if not self._api.repo_exists(repo_id=self._repo_id, repo_type=self._repo_type):
            raise FileNotFoundError(
                f"Hugging Face {self._repo_type} repo '{self._repo_id}' does not exist. "
                "Create it on HF with your intended visibility (public or private), "
                "then re-run. roar does not create repos, just as it does not create "
                "S3 buckets — storage visibility is yours to set, not roar's to guess."
            )
        self._repo_checked = True

    def _full_key(self, remote_key: str) -> str:
        return f"{self._prefix}/{remote_key}" if self._prefix else remote_key

    def _resolve_url(self, full_key: str) -> str:
        # model -> huggingface.co/<repo>/resolve/main/<key>
        # dataset/space -> huggingface.co/datasets|spaces/<repo>/resolve/main/<key>
        seg = "" if self._repo_type == "model" else f"{self._repo_type}s/"
        return f"{_HF}/{seg}{self._repo_id}/resolve/main/{full_key}"

    def upload(self, local_path: Path, remote_key: str) -> str:
        self._ensure_repo_exists()
        full_key = self._full_key(remote_key)
        self._api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=full_key,
            repo_id=self._repo_id,
            repo_type=self._repo_type,
        )
        return self._resolve_url(full_key)

    def exists(self, remote_key: str) -> bool:
        try:
            return bool(
                self._api.file_exists(
                    repo_id=self._repo_id,
                    filename=self._full_key(remote_key),
                    repo_type=self._repo_type,
                )
            )
        except Exception:
            return False
