"""
CLI commands for tracer backend configuration and diagnostics.

Usage: roar tracer [status|set-default|check|setup ebpf]
"""

import shutil
import subprocess
from pathlib import Path

import click

from ...config import config_get, config_set

REQUIRED_CAPS = "cap_bpf,cap_perfmon,cap_sys_resource,cap_sys_ptrace,cap_dac_read_search+ep"
EXPECTED_CAP_NAMES = {
    "cap_bpf",
    "cap_dac_read_search",
    "cap_perfmon",
    "cap_sys_ptrace",
    "cap_sys_resource",
}


def _find_ebpf_tracer() -> str | None:
    """Find the roar-tracer-ebpf binary."""
    package_path = Path(__file__).parent.parent.parent
    candidates = [
        package_path.parent / "tracer-ebpf" / "target" / "release" / "roar-tracer-ebpf",
        package_path / "bin" / "roar-tracer-ebpf",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return shutil.which("roar-tracer-ebpf")


def _find_roard() -> str | None:
    """Find the roard daemon binary."""
    package_path = Path(__file__).parent.parent.parent
    candidates = [
        package_path.parent / "tracer-ebpf" / "target" / "release" / "roard",
        package_path / "bin" / "roard",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return shutil.which("roard")


def _find_ptrace_tracer() -> str | None:
    """Find the roar-tracer (ptrace) binary."""
    package_path = Path(__file__).parent.parent.parent
    candidates = [
        package_path.parent / "tracer" / "target" / "release" / "roar-tracer",
        package_path / "bin" / "roar-tracer",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return shutil.which("roar-tracer")


def _get_current_caps(path: str) -> set[str]:
    """Get current Linux capabilities set on a binary."""
    if not shutil.which("getcap"):
        return set()
    try:
        result = subprocess.run(["getcap", path], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                caps_str = parts[-1].split("=")[0]
                return {c.strip() for c in caps_str.split(",")}
    except Exception:
        pass
    return set()


def _get_perf_event_paranoid() -> int | None:
    """Read current perf_event_paranoid sysctl value."""
    try:
        value = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
        return int(value)
    except Exception:
        return None


def _get_default_mode() -> str:
    """Get configured default tracer mode."""
    return config_get("tracer.default") or "auto"


def _set_tracer_default(mode: str) -> None:
    """Set default tracer mode."""
    try:
        config_path, _ = config_set("tracer.default", mode)
        click.echo(f"Default tracer set to: {mode}")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


def _ebpf_readiness(path: str) -> tuple[bool, str]:
    """Check eBPF readiness and return (ok, reason)."""
    caps = _get_current_caps(path)
    if not caps:
        return False, "no capabilities set"
    missing = EXPECTED_CAP_NAMES - caps
    if missing:
        return False, f"missing {', '.join(sorted(missing))}"

    paranoid = _get_perf_event_paranoid()
    if paranoid is not None and paranoid > 1:
        return False, f"perf_event_paranoid={paranoid} (needs <= 1)"

    return True, "ready"


def _backend_ready(backend: str) -> tuple[bool, str]:
    """Check readiness for backend: auto|ptrace|ebpf."""
    if backend == "ptrace":
        ptrace = _find_ptrace_tracer()
        return (True, ptrace) if ptrace else (False, "ptrace tracer not found")

    if backend == "ebpf":
        ebpf = _find_ebpf_tracer()
        if not ebpf:
            return False, "eBPF tracer not found"
        ok, reason = _ebpf_readiness(ebpf)
        return ok, reason

    # auto: eBPF if ready, otherwise ptrace
    ebpf = _find_ebpf_tracer()
    if ebpf:
        ok, _reason = _ebpf_readiness(ebpf)
        if ok:
            return True, "eBPF ready"
    ptrace = _find_ptrace_tracer()
    if ptrace:
        return True, "ptrace available"
    return False, "no usable tracer found (eBPF not ready, ptrace not found)"


def _print_status() -> None:
    """Print tracer status and environment diagnostics."""
    mode = _get_default_mode()
    fallback = config_get("tracer.fallback_enabled")
    proxy_enabled = config_get("proxy.enabled")
    if fallback is None:
        fallback = True
    if proxy_enabled is None:
        proxy_enabled = False

    click.echo(f"Default tracer: {mode}")
    click.echo(f"Fallback enabled: {fallback}")
    click.echo(f"Proxy enabled: {proxy_enabled}")
    click.echo("")

    ptrace = _find_ptrace_tracer()
    if ptrace:
        click.echo(f"  ptrace:  {ptrace}")
    else:
        click.echo("  ptrace:  not found")

    ebpf = _find_ebpf_tracer()
    if ebpf:
        ok, reason = _ebpf_readiness(ebpf)
        status = "ready" if ok else reason
        click.echo(f"  ebpf:    {ebpf} ({status})")
    else:
        click.echo("  ebpf:    not found")

    roard = _find_roard()
    if roard:
        click.echo(f"  roard:   {roard}")
    else:
        click.echo("  roard:   not found")

    paranoid = _get_perf_event_paranoid()
    if paranoid is not None:
        status = "ok" if paranoid <= 1 else "too restrictive (needs <= 1)"
        click.echo(f"  perf_event_paranoid: {paranoid} ({status})")


@click.group("tracer", invoke_without_command=True)
@click.pass_context
def tracer(ctx: click.Context) -> None:
    """Manage tracer backend defaults and diagnostics."""
    if ctx.invoked_subcommand is None:
        _print_status()


@tracer.command("status")
def tracer_status() -> None:
    """Show tracer status."""
    _print_status()


@tracer.command("set-default")
@click.argument("mode", type=click.Choice(["auto", "ebpf", "ptrace"]))
def tracer_set_default(mode: str) -> None:
    """Set default tracer backend policy."""
    _set_tracer_default(mode)


@tracer.command("auto")
def tracer_auto() -> None:
    """Convenience alias for set-default auto."""
    _set_tracer_default("auto")


@tracer.command("ebpf")
def tracer_ebpf() -> None:
    """Convenience alias for set-default ebpf."""
    _set_tracer_default("ebpf")


@tracer.command("ptrace")
def tracer_ptrace() -> None:
    """Convenience alias for set-default ptrace."""
    _set_tracer_default("ptrace")


@tracer.command("check")
@click.option(
    "--backend",
    type=click.Choice(["auto", "ebpf", "ptrace"]),
    default=None,
    help="Backend policy to validate (defaults to configured default tracer).",
)
def tracer_check(backend: str | None) -> None:
    """Validate tracer backend readiness (non-zero exit if not ready)."""
    target = backend or _get_default_mode()
    ok, detail = _backend_ready(target)
    if ok:
        click.echo(f"Tracer check passed for '{target}': {detail}")
        return

    click.echo(f"Tracer check failed for '{target}': {detail}", err=True)
    raise SystemExit(1)


@tracer.group("setup", invoke_without_command=True)
@click.pass_context
def tracer_setup(ctx: click.Context) -> None:
    """Set up tracer backends."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@tracer_setup.command("ebpf")
@click.option(
    "--path",
    "binary_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to roar-tracer-ebpf binary (overrides auto-detection).",
)
def tracer_setup_ebpf(binary_path: str | None) -> None:
    """Set up eBPF tracer capabilities (needs sudo)."""
    if binary_path:
        ebpf = str(Path(binary_path).resolve())
    else:
        ebpf = _find_ebpf_tracer()

    if not ebpf:
        click.echo("Error: roar-tracer-ebpf binary not found.", err=True)
        click.echo("", err=True)
        click.echo("Build it with:", err=True)
        click.echo("  cd tracer-ebpf && cargo build --release", err=True)
        raise SystemExit(1)

    click.echo(f"Binary: {ebpf}")
    current_caps = _get_current_caps(ebpf)

    if EXPECTED_CAP_NAMES.issubset(current_caps):
        click.echo("Capabilities: already configured")
    else:
        if current_caps:
            missing = EXPECTED_CAP_NAMES - current_caps
            click.echo(f"Capabilities: missing {', '.join(sorted(missing))}")
        else:
            click.echo("Capabilities: not set")

        setcap_cmd = ["sudo", "setcap", REQUIRED_CAPS, ebpf]
        click.echo(f"Running: {' '.join(setcap_cmd)}")
        result = subprocess.run(setcap_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            click.echo("")
            if "operation not supported" in stderr.lower():
                click.echo(
                    "Error: Filesystem does not support capabilities (network/FUSE mount?).",
                    err=True,
                )
                click.echo("Copy the binary to a local path first:", err=True)
                click.echo(f"  sudo cp {ebpf} /usr/local/bin/roar-tracer-ebpf", err=True)
                click.echo(
                    "  roar tracer setup ebpf --path /usr/local/bin/roar-tracer-ebpf",
                    err=True,
                )
            else:
                if stderr:
                    click.echo(stderr, err=True)
                click.echo("Failed to set capabilities. Run manually:", err=True)
                click.echo(f"  sudo setcap '{REQUIRED_CAPS}' {ebpf}", err=True)
            raise SystemExit(1)

        new_caps = _get_current_caps(ebpf)
        if EXPECTED_CAP_NAMES.issubset(new_caps):
            click.echo("Capabilities: set successfully")
        else:
            click.echo("Warning: setcap succeeded but verification failed.", err=True)
            raise SystemExit(1)

    paranoid = _get_perf_event_paranoid()
    if paranoid is None:
        click.echo("perf_event_paranoid: could not read (non-Linux?)")
    elif paranoid <= 1:
        click.echo(f"perf_event_paranoid: {paranoid} (ok)")
    else:
        click.echo(f"perf_event_paranoid: {paranoid} (too restrictive, needs <= 1)")
        click.echo("")
        click.echo("Fix for current session:")
        click.echo("  sudo sysctl kernel.perf_event_paranoid=1")
        click.echo("")
        click.echo("Fix permanently (survives reboot):")
        click.echo('  echo "kernel.perf_event_paranoid=1" | sudo tee /etc/sysctl.d/99-ebpf-tracer.conf')
        click.echo("  sudo sysctl --system")
        raise SystemExit(1)

    click.echo("")
    click.echo("eBPF tracer is ready.")
