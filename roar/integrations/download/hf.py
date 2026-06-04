"""Hugging Face Hub download backend for ``roar get hf://...``.

URL form::

    hf://datasets/<owner>/<name>[@<ref>][/<subpath>]
    hf://<owner>/<name>[@<ref>][/<subpath>]          # model repo

The backend resolves a floating ``ref`` (default ``main``) to an immutable commit
SHA and pins every operation to it. File listing comes from the HF tree API, which
exposes per-file LFS sha256 (the ``lfs.oid``) without downloading bytes — so a
dataset's content identity is computable from metadata. Downloads stream from the
``resolve`` endpoint and are verified against the published sha256.

stdlib-only (urllib); no huggingface_hub dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .base import DownloadBackend, DownloadError, Source

_HF = "https://huggingface.co"


@dataclass(frozen=True)
class HFFileMeta:
    """One file in an HF repo, from the tree API (no bytes downloaded)."""

    path: str  # repo-relative POSIX path
    size: int
    is_lfs: bool
    sha256: str | None  # LFS oid (== sha256 of content); None for non-LFS git blobs
    git_oid: str  # git blob SHA-1 (always present)


def _token() -> str | None:
    p = Path("~/.hf_token").expanduser()
    if p.exists():
        return p.read_text().strip()
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def parse_hf_url(url: str) -> tuple[str, str, str, str]:
    """Parse ``hf://...`` into ``(repo_type, repo, ref, subpath)``.

    ``repo_type`` is ``datasets`` (default) or ``models``; ``repo`` is ``owner/name``;
    ``ref`` defaults to ``main``; ``subpath`` is the within-repo path (may be empty).
    """
    if not url.startswith("hf://"):
        raise ValueError(f"not an hf:// url: {url!r}")
    rest = url[len("hf://") :]
    repo_type = "datasets"
    for prefix, kind in (("datasets/", "datasets"), ("models/", "models"), ("spaces/", "spaces")):
        if rest.startswith(prefix):
            repo_type = kind
            rest = rest[len(prefix) :]
            break
    # split off @ref (applies to the repo, before any subpath)
    ref = "main"
    segments = rest.split("/")
    if len(segments) < 2:
        raise ValueError(f"hf:// url must include owner/name: {url!r}")
    owner = segments[0]
    name = segments[1]
    subpath_segments = segments[2:]
    if "@" in name:
        name, ref = name.split("@", 1)
    repo = f"{owner}/{name}"
    subpath = "/".join(s for s in subpath_segments if s)
    return repo_type, repo, ref, subpath


class HFDownloadBackend(DownloadBackend):
    """Download backend for Hugging Face Hub dataset/model repos."""

    def __init__(self, source: Source) -> None:
        self._repo_type, self._repo, self._ref, self._subpath = parse_hf_url(source.original_url)
        self._token = _token()
        self._commit: str | None = None
        self._manifest_cache: list[HFFileMeta] | None = None

    # --- HTTP helpers -----------------------------------------------------
    def _request(self, url: str) -> urllib.request.Request:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        return urllib.request.Request(url, headers=headers)

    def _get_json(self, url: str):
        try:
            with urllib.request.urlopen(self._request(url), timeout=60) as resp:
                return json.loads(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raise self._describe_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise DownloadError(f"Could not reach Hugging Face at {_HF}: {exc.reason}") from exc

    def _describe_http_error(self, exc: urllib.error.HTTPError) -> DownloadError:
        """Turn an HF API HTTP error into an actionable, user-facing message."""
        kind = self._repo_type.rstrip("s")  # datasets -> dataset, models -> model
        if exc.code == 404:
            return DownloadError(
                f"Hugging Face {kind} not found: '{self._repo}' (ref '{self._ref}').\n"
                f"Check the hf:// URL — expected 'hf://{self._repo_type}/<owner>/<name>' "
                "(or 'hf://<owner>/<name>' for a model), with no 'huggingface.co' host "
                f"segment in the path. Parsed repo from your URL: '{self._repo}'."
            )
        if exc.code in (401, 403):
            hint = (
                "It may be private or gated — request access on the Hub."
                if self._token is not None
                else "If it's private or gated, set HF_TOKEN (or ~/.hf_token) and retry."
            )
            return DownloadError(
                f"Access denied (HTTP {exc.code}) for Hugging Face {kind} '{self._repo}'. {hint}"
            )
        return DownloadError(
            f"Hugging Face API error (HTTP {exc.code}) for {kind} '{self._repo}' "
            f"(ref '{self._ref}')."
        )

    # --- pinning + listing ------------------------------------------------
    @property
    def commit(self) -> str:
        if self._commit is None:
            body, _ = self._get_json(
                f"{_HF}/api/{self._repo_type}/{self._repo}/revision/{self._ref}"
            )
            self._commit = str(body["sha"])
        return self._commit

    @property
    def coordinates(self) -> dict[str, str]:
        """Host coordinates for roar labels (not part of the content key)."""
        return {
            "host": "hf",
            "repo_type": self._repo_type,
            "repo": self._repo,
            "ref": self._ref,
            "commit": self.commit,
        }

    def manifest(self) -> list[HFFileMeta]:
        """Full recursive file manifest at the pinned commit (paginated)."""
        if self._manifest_cache is not None:
            return self._manifest_cache
        url: str | None = f"{_HF}/api/{self._repo_type}/{self._repo}/tree/{self.commit}?recursive=1"
        files: list[HFFileMeta] = []
        while url:
            entries, headers = self._get_json(url)
            for e in entries:
                if e.get("type") != "file":
                    continue
                lfs = e.get("lfs")
                files.append(
                    HFFileMeta(
                        path=e["path"],
                        size=(lfs or {}).get("size", e.get("size", 0)),
                        is_lfs=bool(lfs),
                        sha256=(lfs or {}).get("oid") if lfs else None,
                        git_oid=e["oid"],
                    )
                )
            link = headers.get("Link") or headers.get("link") or ""
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1 : part.find(">")]
                    break
        self._manifest_cache = files
        return files

    # --- DownloadBackend interface ---------------------------------------
    def list_keys(self, prefix: str) -> list[str]:
        scope = self._subpath or prefix.lstrip("/")
        keys = [f.path for f in self.manifest()]
        if scope:
            scope = scope.rstrip("/")
            keys = [k for k in keys if k == scope or k.startswith(scope + "/")]
        return sorted(keys)

    def exists(self, remote_key: str) -> bool:
        try:
            url = f"{_HF}/{self._repo_type}/{self._repo}/resolve/{self.commit}/{remote_key}"
            req = self._request(url)
            req.method = "HEAD"
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise
        except Exception:
            return False

    def download(self, remote_key: str, local_path: Path) -> None:
        url = f"{_HF}/{self._repo_type}/{self._repo}/resolve/{self.commit}/{remote_key}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(self._request(url), timeout=600) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(f"hf://{self._repo}@{self.commit}/{remote_key}") from exc
            raise OSError(f"failed to download {remote_key}: HTTP {exc.code}") from exc

        # Verify against the published LFS sha256 (asserted -> verified). Tamper
        # evidence: a CDN/content mismatch fails loudly rather than silently landing.
        expected = self._sha256_by_path().get(remote_key)
        if expected is not None:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise OSError(
                    f"sha256 mismatch for {remote_key}: expected {expected}, got {actual}"
                )
        local_path.write_bytes(data)

    def _sha256_by_path(self) -> dict[str, str]:
        return {f.path: f.sha256 for f in self.manifest() if f.sha256}

    def sha256_of(self, remote_key: str) -> str:
        """Download a (typically small, non-LFS) file and return its content sha256.

        Used to fold identity-bearing non-LFS files (e.g. LeRobot ``meta/``) into a
        composite at a uniform sha256 — HF only publishes sha256 for LFS files.
        """
        url = f"{_HF}/{self._repo_type}/{self._repo}/resolve/{self.commit}/{remote_key}"
        with urllib.request.urlopen(self._request(url), timeout=120) as resp:
            return hashlib.sha256(resp.read()).hexdigest()
