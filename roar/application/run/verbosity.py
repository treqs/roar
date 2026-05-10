"""Verbosity resolution for `roar run`.

Resolves the effective output verbosity (`quiet | normal | verbose | debug`)
from CLI flags + config, with deterministic precedence and a soft
deprecation path for the legacy `output.quiet` boolean config key.

Precedence (highest first):
  1. CLI: `-q/--quiet` (mutually exclusive with) `-v` / `-vv`
  2. Config: `output.verbosity` if explicitly set
  3. Config: `output.quiet` if explicitly set (logs a one-time deprecation note)
  4. Default: `"normal"`
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Literal

import click

Verbosity = Literal["quiet", "normal", "verbose", "debug"]
_VALID_VERBOSITY: tuple[Verbosity, ...] = ("quiet", "normal", "verbose", "debug")

_QUIET_DEPRECATION_WARNED = False


def _read_raw_output_section(repo_root: str | Path | None) -> dict[str, object]:
    """Read just the [output] section of .roar/config.toml as raw TOML.

    We need the raw form (not the validated `OutputConfig` defaults) to
    distinguish "user explicitly set X" from "field defaulted to X".
    """
    if repo_root is None:
        return {}
    try:
        try:
            import tomllib as _tomllib
        except ImportError:
            import tomli as _tomllib  # type: ignore[no-redef]

        config_toml = Path(repo_root) / ".roar" / "config.toml"
        if not config_toml.exists():
            return {}
        data = _tomllib.loads(config_toml.read_text())
    except Exception:
        return {}
    output = data.get("output")
    return output if isinstance(output, dict) else {}


def _maybe_warn_quiet_deprecated(stream: IO[str] | None) -> None:
    global _QUIET_DEPRECATION_WARNED
    if _QUIET_DEPRECATION_WARNED:
        return
    _QUIET_DEPRECATION_WARNED = True
    target = stream if stream is not None else sys.stderr
    target.write(
        "warning: output.quiet is deprecated; use output.verbosity = "
        '"quiet" | "normal" | "verbose" | "debug" instead.\n'
    )
    target.flush()


def resolve_verbosity(
    *,
    cli_quiet: bool,
    cli_verbose: int,
    repo_root: str | Path | None,
    stream: IO[str] | None = None,
) -> Verbosity:
    """Compute the effective verbosity for one `roar run` invocation.

    Args:
        cli_quiet: True if user passed `-q` / `--quiet`.
        cli_verbose: Count of `-v` flags (0, 1, 2+). 1 → verbose, 2+ → debug.
        repo_root: Path to the repo root, used to read `.roar/config.toml`.
        stream: Override for deprecation-message destination (defaults to stderr).
    """
    if cli_quiet and cli_verbose:
        raise click.UsageError("Cannot combine --quiet and --verbose.")
    if cli_quiet:
        return "quiet"
    if cli_verbose >= 2:
        return "debug"
    if cli_verbose == 1:
        return "verbose"

    raw = _read_raw_output_section(repo_root)
    if "verbosity" in raw:
        v = raw["verbosity"]
        if v not in _VALID_VERBOSITY:
            raise click.UsageError(
                f"Invalid output.verbosity in config: {v!r}. "
                f"Valid values: {', '.join(_VALID_VERBOSITY)}."
            )
        return v  # type: ignore[return-value]
    if "quiet" in raw:
        # Only flag deprecation when quiet=true is actually load-bearing.
        # `quiet = false` matches the default and may be present in older
        # init templates — no need to nag those users.
        if raw["quiet"]:
            _maybe_warn_quiet_deprecated(stream)
            return "quiet"
        return "normal"
    return "normal"


# Public for tests: reset the one-shot deprecation latch between cases.
def _reset_deprecation_latch() -> None:
    global _QUIET_DEPRECATION_WARNED
    _QUIET_DEPRECATION_WARNED = False
