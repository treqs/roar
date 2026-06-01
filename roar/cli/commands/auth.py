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

from ...integrations.config import config_get


def _glaas_web_url() -> str:
    """The GLaaS web UI URL for sign-in / key-management guidance.

    Derived from config (``glaas.web_url``, else inferred from ``glaas.url``)
    so guidance points at the same dev/staging deployment the CLI talks to,
    instead of hardcoding prod. Falls back to ``https://glaas.ai``.
    """
    from ...integrations.config.raw import get_raw_glaas_web_url

    return get_raw_glaas_web_url(start_dir=os.getcwd()) or "https://glaas.ai"


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
    """Manage SSH-key authentication with GLaaS.

    \b
    Most users should just run 'roar login' (browser sign-in — no key
    handling). Use this SSH-key flow for CI or headless machines where a
    browser isn't available:
        1. Run 'roar auth key' to display your public key
        2. Sign in at GLaaS (via GitHub) and add the key in your account settings
        3. Run 'roar auth test' to verify

    \b
    Examples:
        roar login            # Recommended: browser sign-in
        roar auth key         # Show your SSH key (CI/headless)
        roar auth test        # Test connection
        roar auth status      # Show auth status
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _show_auth_key() -> None:
    """Render the SSH public key guidance used by auth key and its legacy alias."""
    key_info = _find_ssh_pubkey()

    if not key_info:
        raise click.ClickException(
            "No SSH public key found.\n\n"
            "Generate one with:\n"
            "  ssh-keygen -t ed25519\n\n"
            "Then run 'roar auth key' again."
        )

    key_type, pubkey, path = key_info
    web_url = _glaas_web_url()
    click.echo("Your SSH public key:")
    click.echo("")
    click.echo(f"  {pubkey}")
    click.echo("")
    click.echo(f"Key type: {key_type}")
    click.echo(f"Path: {path}")
    click.echo("")
    click.echo(f"To register it: sign in at {web_url}/login (via GitHub), then add")
    click.echo("this key in your account settings. Verify with 'roar auth test'.")
    click.echo("")
    click.echo("Most users don't need this — 'roar login' signs in via browser instead.")


def _extract_auth_error_detail(error_body: str, fallback: str) -> str:
    try:
        error_data = json.loads(error_body)
    except Exception:
        return fallback

    if isinstance(error_data, dict):
        detail = error_data.get("detail") or error_data.get("message")
        nested_error = error_data.get("error")
        if not detail and isinstance(nested_error, dict):
            detail = nested_error.get("detail") or nested_error.get("message")
        if detail:
            return str(detail)

    return fallback


@auth.command("key")
def auth_key() -> None:
    """Show SSH public key for GLaaS signup."""
    _show_auth_key()


@auth.command("register", hidden=True)
def auth_register() -> None:
    """Backward-compatible alias for 'roar auth key'."""
    _show_auth_key()


@auth.command("test")
def auth_test() -> None:
    """Test connection to GLaaS server."""
    from ...integrations.glaas import get_glaas_url

    glaas_url = get_glaas_url()

    if not glaas_url:
        raise click.ClickException(
            "GLaaS server URL not configured.\n\n"
            "Set it with:\n"
            "  roar config set glaas.url https://glaas.example.com\n\n"
            "Or set GLAAS_URL environment variable."
        )

    click.echo(f"Testing connection to {glaas_url}...")

    # Try health endpoint (no auth required)
    try:
        health_url = f"{glaas_url.rstrip('/')}/api/v1/health"
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                click.echo("Server is reachable.")
            else:
                raise click.ClickException(f"Server returned status {resp.status}")
    except urllib.error.URLError as e:
        raise click.ClickException(f"Failed to connect: {e}") from e

    # Test authenticated endpoint
    click.echo("Testing authentication...")

    from ...integrations.glaas import compute_pubkey_fingerprint, make_auth_header
    from ...integrations.glaas import find_ssh_pubkey as glaas_find_ssh_pubkey

    key_info = glaas_find_ssh_pubkey()
    if not key_info:
        raise click.ClickException("No SSH key found. Run 'roar auth key' first.")

    _, pubkey, key_path = key_info
    fingerprint = compute_pubkey_fingerprint(pubkey)
    click.echo(f"Using key: {key_path}")
    click.echo(f"Fingerprint: {fingerprint}")

    # Probe a route that requires real auth so anonymous access cannot look successful.
    test_path = "/api/v1/sessions?limit=1"
    auth_header = make_auth_header("GET", test_path, None)

    if not auth_header:
        raise click.ClickException("Failed to create signature. Check your SSH key.")

    try:
        test_url = f"{glaas_url.rstrip('/')}{test_path}"
        req = urllib.request.Request(test_url)
        req.add_header("Authorization", auth_header)

        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                click.echo("Authentication successful!")
            else:
                raise click.ClickException(f"Server returned status {resp.status}")

    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Try to get error detail
            try:
                error_body = e.read().decode()
                detail = _extract_auth_error_detail(error_body, "Unknown error")
            except Exception:
                detail = str(e)
            web_url = _glaas_web_url()
            raise click.ClickException(
                f"Authentication failed: {detail}\n\n"
                "Your key isn't registered with GLaaS. Two ways to fix:\n"
                "  1. Run `roar login` to sign in via browser (recommended).\n"
                f"  2. Or sign in at {web_url}/login (via GitHub) and add this "
                "key in your account settings."
            ) from e
        else:
            raise click.ClickException(f"Server error: {e.code}") from e

    except urllib.error.URLError as e:
        raise click.ClickException(f"Connection failed: {e}") from e


@auth.command("status")
def auth_status() -> None:
    """Show current auth status."""
    from ...integrations.glaas import get_glaas_url

    glaas_url = get_glaas_url()
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
