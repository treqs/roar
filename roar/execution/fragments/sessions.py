from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path


def load_fragment_session(roar_dir: Path, session_id: str) -> dict[str, Any]:
    path = fragment_session_path(roar_dir, session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid fragment session payload in {path}")
    return payload
