"""Utilities for computing canonical local DAG/session hashes."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


def compute_local_session_hash(
    *,
    roar_dir: str | Path,
    session_id: int | None,
    fallback_suffix: str | None = None,
) -> str:
    """Compute the canonical local DAG hash used for registration and lookup."""
    roar_dir_path = Path(roar_dir)

    if session_id is not None:
        session_id_str = f"{roar_dir_path}:{session_id}"
    elif fallback_suffix:
        session_id_str = f"{roar_dir_path}:{fallback_suffix}"
    else:
        session_id_str = f"{roar_dir_path}:external:{time.time()}"

    return hashlib.sha256(session_id_str.encode()).hexdigest()
