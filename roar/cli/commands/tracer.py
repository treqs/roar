"""
CLI commands for tracer backend configuration and diagnostics.

Usage: roar tracer [status|set-default|check|setup ebpf]
"""

import json
import subprocess
from pathlib import Path

import click

from ...core.tracer_modes import TRACER_MODE_VALUES, is_valid_tracer_mode
from ...execution.runtime import tracer_backends
from ...integrations.config import config_get, config_set

REQUIRED_CAPS = "cap_bpf,cap_perfmon,cap_sys_resource,cap_sys_ptrace,cap_dac_read_search+ep"
EXPECTED_CAP_NAMES = tracer_backends.EXPECTED_EBPF_CAP_NAMES


def _package_path() -> Path:
    """Resolve roar package root path."""
    return Path(__file__).parent.parent.parent


def _find_ebpf_tracer() -> str | None:
    """Find the roar-tracer-ebpf binary."""
    return tracer_backends.find_ebpf_tracer(_package_path())


def _find_roard() -> str | None:
    """Find the roard daemon binary."""
    return tracer_backends.find_roard(_package_path())


def _find_ptrace_tracer() -> str | None:
    """Find the roar-tracer (ptrace) binary."""
    return tracer_backends.find_ptrace_tracer(_package_path())


def _find_preload_tracer() -> str | None:
    """Find the roar-tracer-preload binary."""
    return tracer_backends.find_preload_tracer(_package_path())


def _find_preload_library() -> str | None:
    """Find preload interposer shared library."""
    return tracer_backends.find_preload_library(_package_path())


def _get_current_caps(path: str) -> set[str]:
    """Get current Linux capabilities set on a binary."""
    caps = tracer_backends.get_binary_caps(path)
    return caps if caps is not None else set()


def _get_perf_event_paranoid() -> int | None:
    """Read current perf_event_paranoid sysctl value."""
    return tracer_backends.get_perf_event_paranoid()


def _get_default_mode() -> str:
    """Get configured default tracer mode."""
    mode = config_get("tracer.default")
    if isinstance(mode, str) and is_valid_tracer_mode(mode):
        return mode
    return "auto"


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
    readiness = tracer_backends.ebpf_readiness(path)
    return readiness.ok, readiness.reason or "ready"


def _preload_readiness(path: str) -> tuple[bool, str]:
    """Check preload readiness and return (ok, reason)."""
    readiness = tracer_backends.preload_readiness(_package_path(), path)
    return readiness.ok, readiness.reason or "ready"


def _ptrace_readiness(path: str) -> tuple[bool, str]:
    """Check ptrace readiness and return (ok, reason)."""
    readiness = tracer_backends.ptrace_readiness(path)
    return readiness.ok, readiness.reason or "ready"


def _backend_preflight(
    backend: str,
    command: str | None,
) -> tracer_backends.AutoPreflightResult | tracer_backends.TracerPreflightResult:
    """Run strict backend preflight."""
    command_args = (command,) if command else None
    if backend == "auto":
        return tracer_backends.preflight_auto_backend(_package_path(), command=command_args)
    return tracer_backends.preflight_backend(_package_path(), backend, command=command_args)


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
        ok, reason = _ptrace_readiness(ptrace)
        status = "ready" if ok else reason
        click.echo(f"  ptrace:  {ptrace} ({status})")
    else:
        click.echo("  ptrace:  not found")

    ebpf = _find_ebpf_tracer()
    if ebpf:
        ok, reason = _ebpf_readiness(ebpf)
        status = "ready" if ok else reason
        click.echo(f"  ebpf:    {ebpf} ({status})")
    else:
        click.echo("  ebpf:    not found")

    preload = _find_preload_tracer()
    if preload:
        ok, reason = _preload_readiness(preload)
        status = "ready" if ok else reason
        preload_lib = _find_preload_library() or "library not found"
        click.echo(f"  preload: {preload} ({status}; lib={preload_lib})")
    else:
        click.echo("  preload: not found")

    roard = _find_roard()
    if roard:
        click.echo(f"  roard:   {roard}")
    else:
        click.echo("  roard:   not found")

    paranoid = _get_perf_event_paranoid()
    if paranoid is not None:
        status = "ok" if paranoid <= 1 else "too restrictive (needs <= 1)"
        click.echo(f"  perf_event_paranoid: {paranoid} ({status})")
        if paranoid > 1:
            click.echo(
                "    → sudo sysctl -w kernel.perf_event_paranoid=1  "
                "(this boot; host-wide; security tradeoff)"
            )
            click.echo(
                "    → echo 'kernel.perf_event_paranoid=1' | sudo tee "
                "/etc/sysctl.d/99-ebpf-tracer.conf  (persistent)"
            )


def _print_single_preflight_result(
    target: str,
    result: tracer_backends.TracerPreflightResult,
) -> None:
    err = not result.ok
    status = "passed" if result.ok else "failed"
    click.echo(f"Tracer check {status} for '{target}': {result.summary}", err=err)
    if result.command_checked:
        click.echo(f"  command: {result.command_checked}", err=err)
    for check in result.checks:
        detail = f" ({check.detail})" if check.detail else ""
        check_status = "ok" if check.ok else "fail"
        click.echo(f"  - {check.name}: {check_status}{detail}", err=err)
    for warning in result.warnings:
        click.echo(f"  ! {warning}", err=err)
    _print_preflight_suggestions(result, err=err)


def _print_auto_preflight_result(target: str, result: tracer_backends.AutoPreflightResult) -> None:
    err = not result.ok
    status = "passed" if result.ok else "failed"
    click.echo(f"Tracer check {status} for '{target}': {result.summary}", err=err)
    if result.command_checked:
        click.echo(f"  command: {result.command_checked}", err=err)
    for backend_result in result.results:
        backend_status = "ok" if backend_result.ok else backend_result.summary
        click.echo(f"  {backend_result.backend}: {backend_status}", err=err)
    _print_preflight_suggestions(result, err=err)


def _print_preflight_suggestions(
    result: tracer_backends.PreflightResult,
    *,
    err: bool,
) -> None:
    suggestions = tracer_backends.suggestions_for_preflight_result(result)
    if not suggestions:
        return
    click.echo("", err=err)
    click.echo("Next steps:", err=err)
    for suggestion in suggestions:
        click.echo(f"  - {suggestion}", err=err)


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
@click.argument("mode", type=click.Choice(list(TRACER_MODE_VALUES)))
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


@tracer.command("preload")
def tracer_preload() -> None:
    """Convenience alias for set-default preload."""
    _set_tracer_default("preload")


@tracer.command("check")
@click.option(
    "--backend",
    type=click.Choice(list(TRACER_MODE_VALUES)),
    default=None,
    help="Backend policy to validate (defaults to configured default tracer).",
)
@click.option(
    "--command",
    default=None,
    help="Optional command name/path for command-aware compatibility checks.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON output.",
)
def tracer_check(backend: str | None, command: str | None, json_output: bool) -> None:
    """Validate tracer backend preflight (non-zero exit if not ready)."""
    target = backend or _get_default_mode()
    result = _backend_preflight(target, command)

    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    elif isinstance(result, tracer_backends.AutoPreflightResult):
        _print_auto_preflight_result(target, result)
    else:
        _print_single_preflight_result(target, result)

    if not result.ok:
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
    ebpf: str | None
    if binary_path:
        ebpf = str(Path(binary_path).resolve())
    else:
        ebpf = _find_ebpf_tracer()

    if not ebpf:
        click.echo("Error: roar-tracer-ebpf binary not found.", err=True)
        click.echo("", err=True)
        click.echo("Build it with:", err=True)
        click.echo("  cd rust && cargo build --release -p roar-tracer-ebpf", err=True)
        raise SystemExit(1)

    assert ebpf is not None
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
        click.echo(
            '  echo "kernel.perf_event_paranoid=1" | sudo tee /etc/sysctl.d/99-ebpf-tracer.conf'
        )
        click.echo("  sudo sysctl --system")
        raise SystemExit(1)

    click.echo("")
    click.echo("eBPF tracer is ready.")
