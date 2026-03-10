"""Authentication helpers for GLaaS API requests."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import time
from pathlib import Path


def _get_logger():
    from ..core.logging import get_logger

    return get_logger()


def get_glaas_url() -> str | None:
    """Get GLaaS server URL from config or environment."""
    from ..config import config_get

    url = os.environ.get("GLAAS_URL")
    if not url:
        url = config_get("glaas.url")
    return url


def _detect_key_type(key_path: Path) -> str:
    """Detect SSH key type from filename or content."""
    name = key_path.name
    if "ed25519" in name:
        return "ed25519"
    if "ecdsa" in name:
        return "ecdsa"
    if "rsa" in name:
        return "rsa"

    content = key_path.read_text()
    if "ed25519" in content.lower():
        return "ed25519"
    if "ecdsa" in content.lower():
        return "ecdsa"
    return "rsa"


def find_ssh_private_key() -> tuple[str, Path] | None:
    """Find SSH private key for signing. Returns (key_type, path) or None."""
    from ..config import config_get

    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        path = Path(env_key)
        if path.exists():
            return _detect_key_type(path), path

    config_key = config_get("glaas.key")
    if config_key:
        path = Path(config_key)
        if path.exists():
            return _detect_key_type(path), path

    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    for key_type, key_name in [("ed25519", "id_ed25519"), ("rsa", "id_rsa"), ("ecdsa", "id_ecdsa")]:
        key_path = ssh_dir / key_name
        if key_path.exists():
            return key_type, key_path
    return None


def find_ssh_pubkey() -> tuple[str, str, Path] | None:
    """Find SSH public key. Returns (key_type, content, path) or None."""
    from ..config import config_get

    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        pubkey_path = Path(env_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return parts[0], content, pubkey_path

    config_key = config_get("glaas.key")
    if config_key:
        pubkey_path = Path(config_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return parts[0], content, pubkey_path

    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    for key_name in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]:
        key_path = ssh_dir / key_name
        if key_path.exists():
            content = key_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return parts[0], content, key_path
    return None


def compute_pubkey_fingerprint(pubkey: str) -> str:
    """Compute SHA256 fingerprint of an SSH public key."""
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid public key format")

    key_data = base64.b64decode(parts[1])
    digest = hashlib.sha256(key_data).digest()
    fingerprint = base64.b64encode(digest).decode().rstrip("=")
    return f"SHA256:{fingerprint}"


def create_signature_payload(
    method: str,
    path: str,
    timestamp: int,
    body_hash: str | None = None,
) -> bytes:
    """Create the payload that gets signed."""
    payload = f"{timestamp}\n{method}\n{path}"
    if body_hash:
        payload += f"\n{body_hash}"
    return payload.encode()


def sign_payload(payload: bytes, key_path: Path, key_type: str) -> bytes | None:
    """
    Sign payload with SSH private key.

    Uses ssh-keygen for signing (available on most systems).
    Returns raw signature bytes or None on failure.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".data") as f:
        f.write(payload)
        payload_path = f.name

    sig_path = payload_path + ".sig"

    try:
        result = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(key_path),
                "-n",
                "glaas",
                payload_path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return None
        if not Path(sig_path).exists():
            return None

        sig_content = Path(sig_path).read_text()
        lines = sig_content.strip().split("\n")
        sig_lines: list[str] = []
        in_sig = False
        for line in lines:
            if line.startswith("-----BEGIN"):
                in_sig = True
                continue
            if line.startswith("-----END"):
                break
            if in_sig:
                sig_lines.append(line)

        if not sig_lines:
            return None

        return base64.b64decode("".join(sig_lines))
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        _get_logger().debug("Failed to sign payload: %s", e)
        return None
    finally:
        with contextlib.suppress(Exception):
            Path(payload_path).unlink()
        with contextlib.suppress(Exception):
            Path(sig_path).unlink()


def make_auth_header(
    method: str,
    path: str,
    body: bytes | None = None,
) -> str | None:
    """Create Authorization header with SSH signature."""
    pubkey_info = find_ssh_pubkey()
    privkey_info = find_ssh_private_key()
    if not pubkey_info or not privkey_info:
        return None

    _, pubkey_content, _ = pubkey_info
    key_type, privkey_path = privkey_info
    fingerprint = compute_pubkey_fingerprint(pubkey_content)
    timestamp = int(time.time())
    body_hash = hashlib.sha256(body).hexdigest() if body else None
    payload = create_signature_payload(method, path, timestamp, body_hash)

    signature = sign_payload(payload, privkey_path, key_type)
    if not signature:
        return None
    sig_b64 = base64.b64encode(signature).decode()
    return f'Signature keyid="{fingerprint}" ts="{timestamp}" sig="{sig_b64}"'
