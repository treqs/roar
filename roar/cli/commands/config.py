"""
Native Click implementation of the config command.

Usage: roar config [list|get|set] [key] [value]
       roar config tracer [status|ebpf|ptrace|auto|setup]
"""

import shutil
import subprocess
from pathlib import Path

import click

from ...config import config_get, config_list, config_set


@click.group("config", invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """View or set configuration.

    Config is stored in .roar/config.toml

    \b
    Examples:

        roar config list                     # List all options

        roar config get registration.omit.enabled  # Get a value

        roar config set output.quiet true    # Set a value
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config.command("list")
def config_list_cmd() -> None:
    """List all config options."""
    keys = config_list()
    click.echo("Available config options:")
    click.echo("")

    for key, info in keys.items():
        default = info["default"]
        desc = info["description"]
        click.echo(f"  {key}")
        click.echo(f"    {desc}")
        click.echo(f"    Default: {default}")
        click.echo("")


@config.command("get")
@click.argument("key")
def config_get_cmd(key: str) -> None:
    """Get a config value.

    Arguments:

        KEY    The config key to get (e.g. registration.omit.enabled)
    """
    value = config_get(key)
    if value is None:
        click.echo(f"{key}: (not set)")
    else:
        click.echo(f"{key}: {value}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set_cmd(key: str, value: str) -> None:
    """Set a config value.

    Arguments:

        KEY    The config key to set

        VALUE  The value to set
    """
    try:
        config_path, typed_value = config_set(key, value)
        click.echo(f"Set {key} = {typed_value}")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


# ── Tracer subgroup ────────────────────────────────────────────────────────

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
    path = shutil.which("roar-tracer-ebpf")
    return path


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
    path = shutil.which("roard")
    return path


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
    path = shutil.which("roar-tracer")
    return path


def _get_current_caps(path: str) -> set[str]:
    """Get the current capabilities set on a binary."""
    try:
        result = subprocess.run(["getcap", path], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                caps_str = parts[-1].split("=")[0]
                return {c.strip() for c in caps_str.split(",")}
    except FileNotFoundError:
        pass
    return set()


def _get_perf_event_paranoid() -> int | None:
    """Read the current perf_event_paranoid sysctl value."""
    try:
        value = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
        return int(value)
    except Exception:
        return None


@config.group("tracer", invoke_without_command=True)
@click.pass_context
def tracer_group(ctx: click.Context) -> None:
    """Configure the tracer backend.

    \b
    Examples:

        roar config tracer           # Show current tracer status
        roar config tracer auto      # Use eBPF if available, else ptrace
        roar config tracer ebpf      # Use eBPF tracer only
        roar config tracer ptrace    # Use ptrace tracer only
        roar config tracer setup     # Set up eBPF capabilities
    """
    if ctx.invoked_subcommand is None:
        _tracer_status()


def _tracer_status() -> None:
    """Show current tracer configuration and status."""
    mode = config_get("tracer.mode") or "auto"
    click.echo(f"Tracer mode: {mode}")
    click.echo("")

    # ptrace tracer
    ptrace = _find_ptrace_tracer()
    if ptrace:
        click.echo(f"  ptrace:  {ptrace}")
    else:
        click.echo("  ptrace:  not found")

    # eBPF tracer
    ebpf = _find_ebpf_tracer()
    if ebpf:
        caps = _get_current_caps(ebpf)
        if EXPECTED_CAP_NAMES.issubset(caps):
            cap_status = "capabilities ok"
        elif caps:
            missing = EXPECTED_CAP_NAMES - caps
            cap_status = f"missing {', '.join(sorted(missing))}"
        else:
            cap_status = "no capabilities set"
        click.echo(f"  ebpf:    {ebpf} ({cap_status})")
    else:
        click.echo("  ebpf:    not found")

    # roard daemon
    roard = _find_roard()
    if roard:
        click.echo(f"  roard:   {roard}")
    else:
        click.echo("  roard:   not found")

    # Kernel setting
    paranoid = _get_perf_event_paranoid()
    if paranoid is not None:
        status = "ok" if paranoid <= 1 else "too restrictive (needs <= 1)"
        click.echo(f"  perf_event_paranoid: {paranoid} ({status})")


def _set_tracer_mode(mode: str) -> None:
    """Set the tracer mode and report."""
    try:
        config_path, _ = config_set("tracer.mode", mode)
        click.echo(f"Tracer mode set to: {mode}")
        click.echo(f"Saved to {config_path}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@tracer_group.command("auto")
def tracer_auto() -> None:
    """Use eBPF tracer if available, fall back to ptrace."""
    _set_tracer_mode("auto")


@tracer_group.command("ebpf")
def tracer_ebpf() -> None:
    """Use eBPF tracer only."""
    _set_tracer_mode("ebpf")


@tracer_group.command("ptrace")
def tracer_ptrace() -> None:
    """Use ptrace tracer only."""
    _set_tracer_mode("ptrace")


@tracer_group.command("setup")
@click.option(
    "--path",
    "binary_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to roar-tracer-ebpf binary (overrides auto-detection).",
)
def tracer_setup(binary_path: str | None) -> None:
    """Set up eBPF tracer capabilities (needs sudo).

    \b
    What this does:
      1. Finds the roar-tracer-ebpf binary
      2. Applies required Linux capabilities (needs sudo)
      3. Checks kernel perf_event_paranoid setting

    \b
    Examples:

        roar config tracer setup
        roar config tracer setup --path /usr/local/bin/roar-tracer-ebpf
    """
    tracer: str | None
    if binary_path:
        tracer = str(Path(binary_path).resolve())
    else:
        tracer = _find_ebpf_tracer()

    if not tracer:
        click.echo("Error: roar-tracer-ebpf binary not found.", err=True)
        click.echo("", err=True)
        click.echo("Build it with:", err=True)
        click.echo("  cd tracer-ebpf && cargo build --release", err=True)
        raise SystemExit(1)

    click.echo(f"Binary: {tracer}")

    # Check current capabilities
    current_caps = _get_current_caps(tracer)

    if EXPECTED_CAP_NAMES.issubset(current_caps):
        click.echo("Capabilities: already configured")
    else:
        if current_caps:
            missing = EXPECTED_CAP_NAMES - current_caps
            click.echo(f"Capabilities: missing {', '.join(sorted(missing))}")
        else:
            click.echo("Capabilities: not set")

        setcap_cmd = ["sudo", "setcap", REQUIRED_CAPS, tracer]
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
                click.echo(
                    f"  sudo cp {tracer} /usr/local/bin/roar-tracer-ebpf",
                    err=True,
                )
                click.echo(
                    "  roar config tracer setup --path /usr/local/bin/roar-tracer-ebpf",
                    err=True,
                )
            else:
                if stderr:
                    click.echo(stderr, err=True)
                click.echo("Failed to set capabilities. Run manually:", err=True)
                click.echo(f"  sudo setcap '{REQUIRED_CAPS}' {tracer}", err=True)
            raise SystemExit(1)

        # Verify
        new_caps = _get_current_caps(tracer)
        if EXPECTED_CAP_NAMES.issubset(new_caps):
            click.echo("Capabilities: set successfully")
        else:
            click.echo(
                "Warning: setcap succeeded but verification failed.",
                err=True,
            )
            raise SystemExit(1)

    # Check perf_event_paranoid
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
        click.echo(
            '  echo "kernel.perf_event_paranoid=1" | sudo tee /etc/sysctl.d/99-ebpf-tracer.conf'
        )
        click.echo("  sudo sysctl --system")
        raise SystemExit(1)

    click.echo("")
    click.echo("eBPF tracer is ready.")
