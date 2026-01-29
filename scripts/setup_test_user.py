#!/usr/bin/env python3
"""Register test user's SSH key in glaas-api database.

Usage:
    # Generate SQL and pipe to psql
    python scripts/setup_test_user.py | psql -h localhost -p 5433 -U postgres -d glaas_test

    # Or just print the SQL
    python scripts/setup_test_user.py
"""

import base64
import hashlib
import sys
from pathlib import Path


def get_pubkey() -> str | None:
    """Find SSH public key from standard locations."""
    for key_type in ["id_ed25519", "id_rsa", "id_ecdsa"]:
        pubkey_path = Path.home() / ".ssh" / f"{key_type}.pub"
        if pubkey_path.exists():
            return pubkey_path.read_text().strip()
    return None


def compute_fingerprint(pubkey: str) -> str:
    """Compute SHA256 fingerprint of SSH pubkey.

    Matches the format used by roar/glaas_client.py:compute_pubkey_fingerprint()
    """
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid public key format")

    key_data = base64.b64decode(parts[1])
    digest = hashlib.sha256(key_data).digest()
    # Remove trailing = to match SSH fingerprint format
    fingerprint = base64.b64encode(digest).decode().rstrip("=")
    return f"SHA256:{fingerprint}"


def main():
    pubkey = get_pubkey()
    if not pubkey:
        print("ERROR: No SSH public key found in ~/.ssh/", file=sys.stderr)
        print("Expected one of: id_ed25519.pub, id_rsa.pub, id_ecdsa.pub", file=sys.stderr)
        sys.exit(1)

    fingerprint = compute_fingerprint(pubkey)
    print(f"-- Registering user with fingerprint: {fingerprint}", file=sys.stderr)

    # Escape single quotes in pubkey for SQL
    pubkey_escaped = pubkey.replace("'", "''")

    sql = f"""
INSERT INTO users (id, pubkey, pubkey_fingerprint, created_at)
VALUES (gen_random_uuid(), '{pubkey_escaped}', '{fingerprint}', NOW())
ON CONFLICT (pubkey_fingerprint) DO NOTHING;
"""
    print(sql)


if __name__ == "__main__":
    main()
