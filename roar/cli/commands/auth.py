"""
Native Click implementation of the auth command.

Usage: roar auth <command>
"""

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import click

from ...config import config_get


def _find_ssh_pubkey() -> tuple[str, str, str] | None:
    """Find an SSH public key. Returns (key_type, pubkey_content, path) or None.

    Priority: ROAR_SSH_KEY env > glaas.key config > ~/.ssh/ default
    """
    # 1. Environment variable - derive pubkey from private key path
    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        pubkey_path = Path(env_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(pubkey_path))

    # 2. Config file - derive pubkey from private key path
    config_key = config_get("glaas.key")
    if config_key:
        pubkey_path = Path(config_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(pubkey_path))

    # 3. Default ~/.ssh/ search
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    # Prefer Ed25519, then RSA
    key_prefs = ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]

    for key_name in key_prefs:
        key_path = ssh_dir / key_name
        if key_path.exists():
            content = key_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(key_path))

    # Check for any .pub file
    for pub_file in ssh_dir.glob("*.pub"):
        content = pub_file.read_text().strip()
        parts = content.split()
        if len(parts) >= 2:
            return (parts[0], content, str(pub_file))

    return None


@click.group("auth", invoke_without_command=True)
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Manage authentication with https://glaas.ai

    \b
    To register with GLaaS:
        1. Run 'roar auth register' to display your public key
        2. Sign up for GLaaS at https://glaas.ai where you can paste your public key
        3. Once added, run 'roar auth test' to verify

    \b
    Examples:
        roar auth register    # Show your SSH key
        roar auth test        # Test connection
        roar auth status      # Show auth status
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@auth.command("register")
def auth_register() -> None:
    """Show SSH public key for registration."""
    key_info = _find_ssh_pubkey()

    if not key_info:
        click.echo("No SSH public key found.")
        click.echo("")
        click.echo("Generate one with:")
        click.echo("  ssh-keygen -t ed25519")
        click.echo("")
        click.echo("Then run 'roar auth register' again.")
        raise SystemExit(1)

    key_type, pubkey, path = key_info
    click.echo("Your SSH public key:")
    click.echo("")
    click.echo(f"  {pubkey}")
    click.echo("")
    click.echo(f"Key type: {key_type}")
    click.echo(f"Path: {path}")
    click.echo("")
    click.echo("Copy and paste this key when you sign up at https://glaas.ai")


@auth.command("test")
def auth_test() -> None:
    """Test connection to GLaaS server."""
    # Get GLaaS server URL from config
    glaas_url = config_get("glaas.url")
    if not glaas_url:
        glaas_url = os.environ.get("GLAAS_URL")

    if not glaas_url:
        click.echo("GLaaS server URL not configured.")
        click.echo("")
        click.echo("Set it with:")
        click.echo("  roar config set glaas.url https://glaas.example.com")
        click.echo("")
        click.echo("Or set GLAAS_URL environment variable.")
        raise SystemExit(1)

    click.echo(f"Testing connection to {glaas_url}...")

    # Try health endpoint (no auth required)
    try:
        health_url = f"{glaas_url.rstrip('/')}/api/v1/health"
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                click.echo("Server is reachable.")
            else:
                click.echo(f"Server returned status {resp.status}")
                raise SystemExit(1)
    except urllib.error.URLError as e:
        click.echo(f"Failed to connect: {e}")
        raise SystemExit(1) from e

    # Test authenticated endpoint
    click.echo("Testing authentication...")

    from ...glaas_client import compute_pubkey_fingerprint, make_auth_header
    from ...glaas_client import find_ssh_pubkey as glaas_find_ssh_pubkey

    key_info = glaas_find_ssh_pubkey()
    if not key_info:
        click.echo("No SSH key found. Run 'roar auth register' first.")
        raise SystemExit(1)

    _, pubkey, key_path = key_info
    fingerprint = compute_pubkey_fingerprint(pubkey)
    click.echo(f"Using key: {key_path}")
    click.echo(f"Fingerprint: {fingerprint}")

    # Try to get a non-existent artifact (will fail with 404 if auth works, 401 if not)
    test_path = "/api/v1/artifacts/00000000"
    auth_header = make_auth_header("GET", test_path, None)

    if not auth_header:
        click.echo("Failed to create signature. Check your SSH key.")
        raise SystemExit(1)

    try:
        test_url = f"{glaas_url.rstrip('/')}{test_path}"
        req = urllib.request.Request(test_url)
        req.add_header("Authorization", auth_header)

        with urllib.request.urlopen(req, timeout=10) as resp:
            # 200 means it found something (unlikely with our dummy hash)
            click.echo("Authentication successful!")

    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 404 = auth worked, artifact just doesn't exist
            click.echo("Authentication successful!")
        elif e.code == 401:
            # Try to get error detail
            try:
                error_body = e.read().decode()
                error_data = json.loads(error_body)
                detail = error_data.get("detail", "Unknown error")
            except Exception:
                detail = str(e)
            click.echo(f"Authentication failed: {detail}")
            click.echo("")
            click.echo("Your key may not be registered with the server.")
            click.echo("Sign up for GLaaS at https://glaas.ai where you can paste your public key.")
            raise SystemExit(1) from e
        else:
            click.echo(f"Server error: {e.code}")
            raise SystemExit(1) from e

    except urllib.error.URLError as e:
        click.echo(f"Connection failed: {e}")
        raise SystemExit(1) from e


@auth.command("status")
def auth_status() -> None:
    """Show current auth status."""
    glaas_url = config_get("glaas.url") or os.environ.get("GLAAS_URL")
    key_info = _find_ssh_pubkey()

    click.echo("GLaaS Auth Status")
    click.echo("=" * 40)
    click.echo(f"Server URL: {glaas_url or '(not configured)'}")
    click.echo(f"SSH key: {key_info[2] if key_info else '(not found)'}")

    if key_info:
        # Compute fingerprint
        parts = key_info[1].split()
        if len(parts) >= 2:
            try:
                key_data = base64.b64decode(parts[1])
                digest = hashlib.sha256(key_data).digest()
                fp = base64.b64encode(digest).decode().rstrip("=")
                click.echo(f"Fingerprint: SHA256:{fp}")
            except Exception:
                pass
