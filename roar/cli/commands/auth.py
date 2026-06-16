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
    from ...integrations.config.raw import get_raw_glaas_web_url

    return get_raw_glaas_web_url(start_dir=os.getcwd()) or "https://glaas.ai"


def _find_ssh_pubkey() -> tuple[str, str, str] | None:
    """Find an SSH public key. Returns (key_type, pubkey_content, path) or None.

    Priority: ROAR_SSH_KEY env > glaas.key config > ~/.ssh/ default
    """
    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        pubkey_path = Path(env_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(pubkey_path))

    config_key = config_get("glaas.key")
    if config_key:
        pubkey_path = Path(config_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(pubkey_path))

    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    for key_name in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]:
        key_path = ssh_dir / key_name
        if key_path.exists():
            content = key_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, str(key_path))

    for pub_file in ssh_dir.glob("*.pub"):
        content = pub_file.read_text().strip()
        parts = content.split()
        if len(parts) >= 2:
            return (parts[0], content, str(pub_file))

    return None


def _ssh_key_source_label() -> str:
    """Human-readable label for how the SSH key was found."""
    if os.environ.get("ROAR_SSH_KEY"):
        return "env: ROAR_SSH_KEY"
    if config_get("glaas.key"):
        return "config: glaas.key"
    return "auto-discovered from ~/.ssh"


def _compute_fingerprint(pubkey_content: str) -> str | None:
    parts = pubkey_content.split()
    if len(parts) < 2:
        return None
    try:
        key_data = base64.b64decode(parts[1])
        digest = hashlib.sha256(key_data).digest()
        fp = base64.b64encode(digest).decode().rstrip("=")
        return f"SHA256:{fp}"
    except Exception:
        return None


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


@click.group("auth", invoke_without_command=True)
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Manage GLaaS authentication.

    \b
    Browser sign-in (recommended for interactive use):
        roar auth login       Sign in via browser and store a token
        roar auth logout      Remove stored token
        roar auth status      Show auth status and test credentials

    \b
    SSH key auth (for CI / headless machines):
        roar auth key         Show your SSH public key for GLaaS signup
        roar auth status      Tests SSH signature in addition to bearer token

    \b
    Examples:
        roar auth login       # Sign in (same as 'roar login')
        roar auth status      # Check which method is active and working
        roar auth key         # Get SSH key for CI setup
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _show_auth_key() -> None:
    key_info = _find_ssh_pubkey()

    if not key_info:
        raise click.ClickException(
            "No SSH public key found.\n\n"
            "Generate one with:\n"
            "  ssh-keygen -t ed25519\n\n"
            "Then run 'roar auth key' again."
        )

    key_type, pubkey, path = key_info
    source = _ssh_key_source_label()
    web_url = _glaas_web_url()
    click.echo("Your SSH public key:")
    click.echo("")
    click.echo(f"  {pubkey}")
    click.echo("")
    click.echo(f"Key type:  {key_type}")
    click.echo(f"Path:      {path}")
    click.echo(f"Source:    {source}")
    click.echo("")
    click.echo(f"To register it: sign in at {web_url}/login (via GitHub), then add")
    click.echo("this key in your account settings. Verify with 'roar auth status'.")
    click.echo("")
    click.echo("Most users don't need this — 'roar auth login' signs in via browser instead.")


@auth.command("key")
def auth_key() -> None:
    """Show SSH public key for GLaaS signup."""
    _show_auth_key()


@auth.command("register", hidden=True)
def auth_register() -> None:
    """Backward-compatible alias for 'roar auth key'."""
    _show_auth_key()


def _do_auth_status() -> None:
    """Shared implementation for 'roar auth status' and its 'test' alias."""
    from ...integrations.glaas import get_glaas_url

    glaas_url = get_glaas_url()
    if not glaas_url:
        raise click.ClickException(
            "GLaaS server URL not configured.\n"
            "Set it with: roar config set glaas.url https://glaas.example.com\n"
            "Or set GLAAS_URL environment variable."
        )

    # Server reachability
    server_reachable = False
    try:
        health_url = f"{glaas_url.rstrip('/')}/api/v1/health"
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            server_reachable = resp.status == 200
    except Exception:
        pass

    server_label = "✓ reachable" if server_reachable else "✗ unreachable"
    click.echo(f"Server: {glaas_url}  {server_label}")
    click.echo("")

    # --- Bearer token (roar login) ---
    from ...auth_store import is_auth_state_expired, load_auth_state

    auth_state = load_auth_state()
    bearer_active = False

    click.echo("Bearer token (roar login / roar auth login):")
    if auth_state is None:
        click.echo("  - Not logged in  ·  run `roar auth login` to sign in via browser")
    elif is_auth_state_expired(auth_state):
        identity = (
            auth_state.user.username or auth_state.user.email or auth_state.user.sub or "unknown"
        )
        click.echo(f"  ✗ Token expired for {identity}  ·  run `roar auth login` to refresh")
    else:
        name = auth_state.user.username or auth_state.user.sub or "unknown"
        email = auth_state.user.email
        identity = f"{name} <{email}>" if email else name
        click.echo(f"  ✓ Logged in as {identity}")
        if auth_state.expires_at:
            click.echo(f"    Expires: {auth_state.expires_at}")
        if server_reachable:
            try:
                test_path = "/api/v1/sessions?limit=1"
                test_url = f"{glaas_url.rstrip('/')}{test_path}"
                req = urllib.request.Request(test_url)
                req.add_header("Authorization", f"Bearer {auth_state.access_token}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        click.echo("  ✓ Token accepted by server")
                        bearer_active = True
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    click.echo("  ✗ Token rejected  ·  run `roar auth login` to refresh")
                else:
                    click.echo(f"  ! Server error {e.code}  (could not verify token)")
            except urllib.error.URLError as e:
                click.echo(f"  ! Connection error: {e}")
        else:
            click.echo("  ! Server unreachable — could not verify token")
    click.echo("")

    # --- SSH key ---
    click.echo("SSH key (roar auth key):")
    key_info = _find_ssh_pubkey()
    ssh_active = False

    if key_info is None:
        click.echo("  - No key found")
        click.echo("    To use SSH auth: ssh-keygen -t ed25519  then  roar auth key")
        click.echo("    (Not needed if using roar auth login)")
    else:
        _, pubkey, path = key_info
        source = _ssh_key_source_label()
        fingerprint = _compute_fingerprint(pubkey)
        click.echo(f"  ✓ Key: {path}  ({source})")
        if fingerprint:
            click.echo(f"    Fingerprint: {fingerprint}")
        if server_reachable:
            from ...integrations.glaas import make_auth_header

            test_path = "/api/v1/sessions?limit=1"
            auth_header = make_auth_header("GET", test_path, None)
            if not auth_header:
                click.echo("  ✗ Could not sign  ·  is the private key present alongside the .pub?")
            else:
                try:
                    test_url = f"{glaas_url.rstrip('/')}{test_path}"
                    req = urllib.request.Request(test_url)
                    req.add_header("Authorization", auth_header)
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        if resp.status == 200:
                            click.echo("  ✓ Signature accepted by server")
                            ssh_active = True
                except urllib.error.HTTPError as e:
                    if e.code == 401:
                        web_url = _glaas_web_url()
                        click.echo("  ✗ Key not registered with GLaaS")
                        click.echo(
                            f"    Register at: {web_url}/login → Account Settings → SSH Keys"
                        )
                    else:
                        click.echo(f"  ! Server error {e.code}")
                except urllib.error.URLError as e:
                    click.echo(f"  ! Connection error: {e}")
        else:
            click.echo("  ! Server unreachable — could not test signature")

    click.echo("")

    # --- Active method summary ---
    if bearer_active:
        click.echo("Active method: bearer token")
        if ssh_active:
            click.echo("  note: SSH key also registered; bearer takes precedence")
    elif ssh_active:
        click.echo("Active method: SSH signature")
        click.echo("  hint: run `roar auth login` for simpler browser-based sign-in")
    elif auth_state is not None or key_info is not None:
        click.echo("Active method: none  (credentials found but none verified against server)")
        if not server_reachable:
            click.echo("  note: server was unreachable; status may differ when connected")
    else:
        click.echo("Active method: none  (not authenticated)")
        click.echo("  Run `roar auth login` to sign in via browser.")

    if server_reachable and not bearer_active and not ssh_active:
        raise SystemExit(1)


@auth.command("status")
def auth_status() -> None:
    """Show auth status and test credentials (like gh auth status)."""
    _do_auth_status()


@auth.command("test", hidden=True)
def auth_test() -> None:
    """Alias for 'roar auth status'."""
    _do_auth_status()


# Expose login / logout inside the auth namespace so users can do
# 'roar auth login' and 'roar auth logout' alongside the top-level aliases.
from .login import login as _login_cmd  # noqa: E402
from .logout import logout as _logout_cmd  # noqa: E402

auth.add_command(_login_cmd, "login")
auth.add_command(_logout_cmd, "logout")
