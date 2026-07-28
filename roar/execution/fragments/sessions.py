from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def resolve_project_roar_dir(
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Locate the project's .roar directory the way the CLI context does.

    Honors ROAR_PROJECT_DIR, then walks upward from cwd looking for an
    existing .roar directory (bounded by the enclosing git repository when
    present); falls back to cwd/.roar. Submit planners must save plan-time
    state (session keys, prepared manifests) here — the run finalizer loads
    the session key from the context-resolved .roar, so saving under a bare
    cwd/.roar strands the key when invoked from a project subdirectory.
    """
    resolved_env = os.environ if environ is None else environ
    override = str(resolved_env.get("ROAR_PROJECT_DIR") or "").strip()
    if override:
        return Path(override) / ".roar"

    base = Path.cwd() if cwd is None else Path(cwd)
    git_root: Path | None = None
    for parent in [base, *base.parents]:
        candidate = parent / ".roar"
        if candidate.is_dir():
            return candidate
        if (parent / ".git").exists():
            git_root = parent
            break
    # On a fresh clone no .roar exists yet: anchor the fallback at the git
    # root, not the bare cwd. A submit planner running in the workflow's
    # working_directory subdir would otherwise save the session key under
    # <subdir>/.roar while the run finalizer (cwd = repo root) loads from
    # <root>/.roar — stranding the key and failing lineage publication.
    return (git_root or base) / ".roar"


def generate_fragment_session() -> dict[str, str]:
    token = secrets.token_bytes(32).hex()
    return {
        "session_id": str(uuid.uuid4()),
        "token": token,
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def fragment_session_path(roar_dir: Path, session_id: str) -> Path:
    return roar_dir / "fragment-sessions" / f"{session_id}.key"


def save_fragment_session(roar_dir: Path, payload: dict[str, Any]) -> Path:
    path = fragment_session_path(roar_dir, str(payload["session_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    # The key file carries the raw fragment token: owner-only from the
    # first byte (write_text would inherit the umask, leaving group/other
    # read), and swapped in atomically so a crash never leaves a partial
    # or over-permissive key behind.
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")))
    os.replace(temp, path)
    return path


def load_fragment_session(roar_dir: Path, session_id: str) -> dict[str, Any]:
    path = fragment_session_path(roar_dir, session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid fragment session payload in {path}")
    return payload
