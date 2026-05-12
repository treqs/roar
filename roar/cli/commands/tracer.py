"""
CLI commands for tracer backend configuration and diagnostics.

Three verbs, one home screen:

    roar tracer              — status + tradeoff table + hints
    roar tracer use <name>   — set the default backend (auto|ebpf|preload|ptrace)
    roar tracer enable ebpf  — guided setup (setcap + sysctl) for the eBPF tracer
    roar tracer check [name] — run the deep preflight for a backend (shows paths)

The home screen leads with what `roar run` would actually pick next (the
live auto resolution), follows with a 3-row table of tradeoffs so the
user can decide, and ends with suppressible amber hints pointing at the
right verb. Paths are diagnostic — they only show up in `check`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from ...core.tracer_modes import TRACER_MODE_VALUES, is_valid_tracer_mode
from ...execution.runtime import tracer_backends
from ...integrations.config import config_get, config_set
from ...presenters.terminal import detect, style
from .._format import hints_should_print, make_hint_printer

REQUIRED_CAPS = "cap_bpf,cap_perfmon,cap_sys_resource,cap_sys_ptrace,cap_dac_read_search+ep"
EXPECTED_CAP_NAMES = tracer_backends.EXPECTED_EBPF_CAP_NAMES


# ---------------------------------------------------------------------------
# Backend specs — single source of truth for the tradeoff table.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendSpec:
    """User-facing description of a single tracer backend.

    `short` is the 1-line tradeoff column. `requirements` summarizes
    permissions. `platforms` gates whether the row is relevant.

    `caveats` and `caveats_darwin` are surfaced as `↳` continuation
    lines under the row — split off from requirements so column 4 stays
    skinny and the heavy preload caveats don't dominate the table.
    """

    name: str
    short: str
    requirements: str
    platforms: tuple[str, ...]
    caveats: tuple[str, ...] = ()
    caveats_darwin: tuple[str, ...] = ()


_SPECS: tuple[BackendSpec, ...] = (
    # Order chosen for the user, not the auto-pick algorithm: preload is
    # the row most users will actually land on (no perms, fast, works for
    # most Python), so it leads. ebpf is the aspirational/fast-path that
    # often needs setup. ptrace is the bottom-of-stack fallback.
    BackendSpec(
        name="preload",
        short="comparable to eBPF",
        requirements="no extra perms",
        platforms=("linux", "darwin"),
        caveats=("skips statically-linked binaries (Go)",),
        caveats_darwin=("skips Apple-signed binaries (/bin/*)",),
    ),
    BackendSpec(
        name="ebpf",
        short="fastest, low overhead",
        requirements="needs CAP_BPF + perf_event_paranoid<=1; Linux only",
        platforms=("linux",),
    ),
    BackendSpec(
        name="ptrace",
        short="slow but correct",
        requirements="no extra perms; works on every binary",
        platforms=("linux",),
    ),
)


# ---------------------------------------------------------------------------
# Helpers — discovery + readiness + availability classification.
# ---------------------------------------------------------------------------


def _package_path() -> Path:
    return Path(__file__).parent.parent.parent


def _find_ebpf_tracer() -> str | None:
    return tracer_backends.find_ebpf_tracer(_package_path())


def _find_ptrace_tracer() -> str | None:
    return tracer_backends.find_ptrace_tracer(_package_path())


def _find_preload_tracer() -> str | None:
    return tracer_backends.find_preload_tracer(_package_path())


def _find_preload_library() -> str | None:
    return tracer_backends.find_preload_library(_package_path())


def _find_roard() -> str | None:
    return tracer_backends.find_roard(_package_path())


def _get_current_caps(path: str) -> set[str]:
    caps = tracer_backends.get_binary_caps(path)
    return caps if caps is not None else set()


def _get_perf_event_paranoid() -> int | None:
    return tracer_backends.get_perf_event_paranoid()


def _get_default_mode() -> str:
    mode = config_get("tracer.default")
    if isinstance(mode, str) and is_valid_tracer_mode(mode):
        return mode
    return "auto"


@dataclass(frozen=True)
class _BackendStatus:
    """Three states for a backend: ready / fixable / unavailable.

    - ready: works right now.
    - fixable: the user could enable it locally (caps, sysctl).
    - unavailable: out of the user's reach on this host (wrong OS,
      kernel missing BTF, locked-down container, binary missing).

    `reason` is human-readable; rendered in the table.
    """

    state: str  # "ready" | "fixable" | "unavailable"
    reason: str | None


def _classify_ebpf() -> _BackendStatus:
    unavail = tracer_backends.ebpf_host_unavailable_reason()
    if unavail:
        return _BackendStatus("unavailable", unavail)
    binary = _find_ebpf_tracer()
    if not binary:
        return _BackendStatus("unavailable", "tracer binary not installed")
    readiness = tracer_backends.ebpf_readiness(binary)
    if readiness.ok:
        return _BackendStatus("ready", None)
    return _BackendStatus("fixable", readiness.reason)


def _classify_preload() -> _BackendStatus:
    binary = _find_preload_tracer()
    if not binary:
        return _BackendStatus("unavailable", "tracer binary not installed")
    readiness = tracer_backends.preload_readiness(_package_path(), binary)
    if readiness.ok:
        return _BackendStatus("ready", None)
    # Preload failures (missing library, etc.) are install-level — count
    # as unavailable rather than fixable, since there's nothing the user
    # can `setcap`/`sysctl` to make it work.
    return _BackendStatus("unavailable", readiness.reason)


def _classify_ptrace() -> _BackendStatus:
    if sys.platform != "linux":
        return _BackendStatus("unavailable", f"{sys.platform} (ptrace is Linux-only)")
    binary = _find_ptrace_tracer()
    if not binary:
        return _BackendStatus("unavailable", "tracer binary not installed")
    readiness = tracer_backends.ptrace_readiness(binary)
    if readiness.ok:
        return _BackendStatus("ready", None)
    return _BackendStatus("unavailable", readiness.reason)


_CLASSIFIERS = {
    "ebpf": _classify_ebpf,
    "preload": _classify_preload,
    "ptrace": _classify_ptrace,
}


def _backend_preflight(
    backend: str,
    command: str | None,
) -> tracer_backends.AutoPreflightResult | tracer_backends.TracerPreflightResult:
    command_args = (command,) if command else None
    if backend == "auto":
        return tracer_backends.preflight_auto_backend(_package_path(), command=command_args)
    return tracer_backends.preflight_backend(_package_path(), backend, command=command_args)


# ---------------------------------------------------------------------------
# Status — the home screen.
# ---------------------------------------------------------------------------


def _print_status() -> None:
    statuses = {name: _CLASSIFIERS[name]() for name in _CLASSIFIERS}
    mode = _get_default_mode()

    # Auto mode: compute the live pick once and reuse below.
    auto_result = (
        tracer_backends.preflight_auto_backend(_package_path(), command=None)
        if mode == "auto"
        else None
    )

    _print_brand_status_line(mode, statuses, auto_result)
    # The per-backend skip reasons that auto saw appear as `↳`
    # continuations inside the table itself, so we don't repeat them
    # here.
    click.echo("")
    _print_backend_table(statuses)

    _print_status_hints(statuses)


def _print_brand_status_line(
    mode: str,
    statuses: dict[str, _BackendStatus],
    auto_result: tracer_backends.AutoPreflightResult | None,
) -> None:
    """`🦖 roar tracer · <active> [· fallback: off]` — the home-screen
    headline. Packs the most decision-relevant facts into one line so the
    persona doesn't have to scan the table to answer "what'll happen on
    my next run?"."""
    caps = detect(sys.stdout)
    active = _describe_active(mode, statuses, auto_result)

    fallback = config_get("tracer.fallback_enabled")
    if fallback is None:
        fallback = True
    fallback_suffix = "" if fallback else "  ·  fallback: off"

    prefix = "🦖" if caps.can_emoji else "roar:"
    line = f"{prefix} roar tracer  ·  active: {active}{fallback_suffix}"
    click.echo(style(line, "status_green", enabled=caps.can_color))


def _describe_active(
    mode: str,
    statuses: dict[str, _BackendStatus],
    auto_result: tracer_backends.AutoPreflightResult | None,
) -> str:
    """One-liner describing what `roar run` will actually do.

    All three branches use a consistent `<name> (<state>[: <detail>])`
    shape so the brand line parses uniformly. For `auto` we additionally
    surface the realized pick via `→`."""
    if mode == "auto":
        if auto_result and auto_result.ok and auto_result.selected_backend:
            return f"auto → {auto_result.selected_backend} (ready)"
        return "auto (no tracer ready)"

    status = statuses.get(mode)
    if status is None:
        return mode
    if status.state == "ready":
        return f"{mode} (ready)"
    if status.state == "fixable":
        return f"{mode} (NOT READY — {status.reason})"
    return f"{mode} (unavailable — {status.reason})"


def _print_backend_table(statuses: dict[str, _BackendStatus]) -> None:
    """4-column tradeoff table with a dim header row. `↳` continuations
    carry per-row caveats and not-ready reasons so the four data columns
    stay scannable."""
    caps = detect(sys.stdout)
    headers = ("Tracer", "Status", "Tradeoff", "Requirements")

    # Pre-render row state labels and gather caveats. Build BEFORE
    # measuring widths so the header is included in the max.
    rendered: list[tuple[str, str, str, str, str, tuple[str, ...]]] = []
    for spec in _SPECS:
        status = statuses[spec.name]
        if status.state == "ready":
            state_label, state_color = "ready", "status_green"
        elif status.state == "fixable":
            state_label, state_color = "not ready", "warn_amber"
        else:
            state_label, state_color = "unavailable", "dim"

        notes: list[str] = []
        if status.reason and status.state != "ready":
            notes.append(status.reason)
        notes.extend(spec.caveats)
        if sys.platform == "darwin":
            notes.extend(spec.caveats_darwin)

        rendered.append(
            (spec.name, state_label, state_color, spec.short, spec.requirements, tuple(notes))
        )

    name_w = max(len(headers[0]), max(len(r[0]) for r in rendered))
    state_w = max(len(headers[1]), max(len(r[1]) for r in rendered))
    short_w = max(len(headers[2]), max(len(r[3]) for r in rendered))
    sep = "  "
    leading = "  "

    # Header row (dim).
    header_line = (
        f"{leading}{headers[0]:<{name_w}}{sep}{headers[1]:<{state_w}}{sep}"
        f"{headers[2]:<{short_w}}{sep}{headers[3]}"
    )
    click.echo(style(header_line, "dim", enabled=caps.can_color))

    # `↳` continuations line up under the Status column — same convention
    # the existing surface used; visually reads as "more about this row".
    cont_indent = leading + " " * name_w + sep

    for name, state_label, state_color, short, requirements, row_notes in rendered:
        state_text = style(state_label, state_color, enabled=caps.can_color)
        state_pad = " " * (state_w - len(state_label))
        click.echo(
            f"{leading}{name:<{name_w}}{sep}{state_text}{state_pad}{sep}"
            f"{short:<{short_w}}{sep}{requirements}"
        )
        for note in row_notes:
            click.echo(style(f"{cont_indent}↳ {note}", "dim", enabled=caps.can_color))


def _print_status_hints(statuses: dict[str, _BackendStatus]) -> None:
    if not hints_should_print():
        return
    _caps, hint = make_hint_printer()
    click.echo("")

    hint("To pick one explicitly: roar tracer use {auto|ebpf|preload|ptrace}")
    hint("To probe one:           roar tracer check {auto|ebpf|preload|ptrace}")
    if statuses["ebpf"].state == "fixable":
        hint(
            "To enable eBPF here:    roar tracer enable ebpf       "
            "(applies setcap + sysctl; needs sudo)"
        )
    hint("Docs: https://glaas.ai/docs/tracers")


# ---------------------------------------------------------------------------
# `use` — set default backend.
# ---------------------------------------------------------------------------


def _set_tracer_default(mode: str) -> None:
    try:
        config_path, _ = config_set("tracer.default", mode)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Default tracer set to: {mode}  (saved to {config_path})")

    # If the user picked something not actually available, tell them now
    # (better here than at the next `roar run`). The cause is a fact; the
    # fix is rendered in the `hint:` amber style — directly actionable,
    # not background education, so we show it regardless of the hints
    # gate.
    if mode == "auto":
        return
    status = _CLASSIFIERS[mode]()
    if status.state == "fixable":
        click.echo(f"  Note: {mode} is not ready right now ({status.reason}).")
        if mode == "ebpf":
            _caps, hint = make_hint_printer()
            hint(f"To fix: roar tracer enable {mode}")
    elif status.state == "unavailable":
        click.echo(f"  Warning: {mode} is unavailable on this host ({status.reason}).")


# ---------------------------------------------------------------------------
# `check` — preflight diagnostic, where paths live.
# ---------------------------------------------------------------------------


def _print_single_preflight_result(
    target: str,
    result: tracer_backends.TracerPreflightResult,
) -> None:
    err = not result.ok
    status = "passed" if result.ok else "failed"
    click.echo(f"Tracer check {status} for '{target}': {result.summary}", err=err)
    if result.command_checked:
        click.echo(f"  command: {result.command_checked}", err=err)
    # Per-check rows carry their own paths inline (e.g. `binary: ok
    # (/path)`); a separate path block would just duplicate them.
    for check in result.checks:
        detail = f" ({check.detail})" if check.detail else ""
        check_status = "ok" if check.ok else "fail"
        click.echo(f"  - {check.name}: {check_status}{detail}", err=err)
    for warning in result.warnings:
        click.echo(f"  ! {warning}", err=err)
    _print_preflight_suggestions(result, err=err)


def _print_auto_preflight_result(
    target: str,
    result: tracer_backends.AutoPreflightResult,
    *,
    command: str | None = None,
) -> None:
    err = not result.ok
    status = "passed" if result.ok else "failed"
    click.echo(f"Tracer check {status} for '{target}': {result.summary}", err=err)
    if result.command_checked:
        click.echo(f"  command: {result.command_checked}", err=err)
    for tracer_result in result.results:
        tracer_status = "ok" if tracer_result.ok else tracer_result.summary
        click.echo(f"  {tracer_result.backend}: {tracer_status}", err=err)

    if result.ok and result.results:
        selected = next((br for br in result.results if br.ok), None)
        if selected is not None:
            deep = _backend_preflight(selected.backend, command)
            if isinstance(deep, tracer_backends.TracerPreflightResult):
                click.echo("")
                click.echo(f"Selected tracer '{selected.backend}': detail")
                for check in deep.checks:
                    detail = f" ({check.detail})" if check.detail else ""
                    check_status = "ok" if check.ok else "fail"
                    click.echo(f"  - {check.name}: {check_status}{detail}")

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


# ---------------------------------------------------------------------------
# Click command group.
# ---------------------------------------------------------------------------


_TRACER_GROUP_EPILOG = """\
\b
Examples:
\b
  roar tracer                Show active tracer + readiness table.
  roar tracer use ebpf       Pick a tracer (auto|ebpf|preload|ptrace).
  roar tracer enable ebpf    Apply host config for a tracer (eBPF only).
  roar tracer check ebpf     Run the deep preflight for a tracer.
"""


@click.group("tracer", invoke_without_command=True, epilog=_TRACER_GROUP_EPILOG)
@click.pass_context
def tracer(ctx: click.Context) -> None:
    """Show tracer status, pick a tracer, or run preflight diagnostics.

    With no subcommand, prints the active tracer and a readiness table
    for ebpf / preload / ptrace.
    """
    if ctx.invoked_subcommand is None:
        _print_status()


@tracer.command("use")
@click.argument("tracer_name", type=click.Choice(list(TRACER_MODE_VALUES)))
def tracer_use(tracer_name: str) -> None:
    """Set the default tracer (auto|ebpf|preload|ptrace)."""
    _set_tracer_default(tracer_name)


@tracer.command("check")
@click.argument(
    "tracer_arg",
    type=click.Choice(list(TRACER_MODE_VALUES)),
    required=False,
    default=None,
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
def tracer_check(
    tracer_arg: str | None,
    command: str | None,
    json_output: bool,
) -> None:
    """Run the deep preflight for a tracer (or the configured default).

    Per-check breakdown with inline paths. With no argument, validates
    the configured default. Non-zero exit if the tracer isn't ready.
    """
    target = tracer_arg or _get_default_mode()
    result = _backend_preflight(target, command)

    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    elif isinstance(result, tracer_backends.AutoPreflightResult):
        _print_auto_preflight_result(target, result, command=command)
    else:
        _print_single_preflight_result(target, result)

    if not result.ok:
        raise SystemExit(1)


@tracer.command("enable")
@click.argument("tracer_name", type=click.Choice(list(TRACER_MODE_VALUES)))
@click.option(
    "--path",
    "binary_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to roar-tracer-ebpf binary (overrides auto-detection).",
)
def tracer_enable(tracer_name: str, binary_path: str | None) -> None:
    """Apply the host config needed for a tracer.

    Only `ebpf` has a guided enable flow today (runs `setcap` and sets
    `kernel.perf_event_paranoid=1` persistently). preload and ptrace
    don't need any host setup — they work out of the box.
    """
    # Accept any tracer name so we can give a clearer message than the
    # raw click choice error for the cases that don't need setup.
    if tracer_name == "ebpf":
        _enable_ebpf(binary_path=binary_path)
        return
    if tracer_name == "auto":
        raise click.UsageError(
            "`auto` is a selection policy, not a tracer — nothing to enable. "
            "Pick a specific tracer (`roar tracer enable ebpf`)."
        )
    # preload, ptrace — they're either ready or unavailable; either way
    # there's no host-config step roar can run.
    click.echo(
        f"{tracer_name} works out of the box — no host setup needed.",
        err=True,
    )
    click.echo(f"Run `roar tracer check {tracer_name}` to verify readiness.", err=True)
    raise SystemExit(2)


def _enable_ebpf(*, binary_path: str | None) -> None:
    unavail = tracer_backends.ebpf_host_unavailable_reason()
    if unavail:
        click.echo(
            f"Error: eBPF is unavailable on this host ({unavail}).",
            err=True,
        )
        click.echo("Use `roar tracer use preload` or `roar tracer use ptrace` instead.", err=True)
        raise SystemExit(1)

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
                    "  roar tracer enable ebpf --path /usr/local/bin/roar-tracer-ebpf",
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
