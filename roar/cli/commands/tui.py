"""Native Rust TUI entrypoint for local lineage browsing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from ..context import RoarContext


@click.command("tui")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Open this .roar/roar.db instead of searching from PATH/current directory.",
)
@click.option(
    "--session", "session_ref", metavar="SESSION", help="Select a session id/hash prefix."
)
@click.option("--job", "job_ref", metavar="JOB", help="Select a job by @N/@BN or job UID prefix.")
@click.option(
    "--artifact",
    "artifact_ref",
    metavar="ARTIFACT",
    help="Select an artifact by id/hash prefix.",
)
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.pass_obj
def tui(
    ctx: RoarContext,
    db_path: Path | None,
    session_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    path: Path | None,
) -> None:
    """Open the read-only Rust lineage explorer.

    The TUI searches upward from PATH (or the current directory) for
    ``.roar/roar.db`` unless ``--db`` is supplied. It is intentionally
    read-only: navigation and preview expansion are the only v1 actions.
    """
    binary = _find_tui_binary()
    if binary is None:
        raise click.ClickException(
            "The roar-tui Rust binary is not available. Reinstall roar-cli or run "
            "`cargo build --manifest-path rust/Cargo.toml -p roar-tui`."
        )

    args = _build_tui_args(
        binary=binary,
        db_path=db_path,
        session_ref=session_ref,
        job_ref=job_ref,
        artifact_ref=artifact_ref,
        path=path,
    )
    env = os.environ.copy()
    env.setdefault("ROAR_TUI_CWD", str(ctx.cwd))
    completed = subprocess.run(args, env=env, check=False)
    raise SystemExit(completed.returncode)


def _build_tui_args(
    *,
    binary: Path,
    db_path: Path | None,
    session_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    path: Path | None,
) -> list[str]:
    args = [str(binary)]
    if db_path is not None:
        args.extend(["--db", str(db_path)])
    if session_ref is not None:
        args.extend(["--session", session_ref])
    if job_ref is not None:
        args.extend(["--job", job_ref])
    if artifact_ref is not None:
        args.extend(["--artifact", artifact_ref])
    if path is not None:
        args.append(str(path))
    return args


def _find_tui_binary() -> Path | None:
    executable = "roar-tui.exe" if sys.platform == "win32" else "roar-tui"
    override = os.environ.get("ROAR_TUI_BIN")
    candidates = [Path(override)] if override else []

    package_binary = Path(__file__).resolve().parents[2] / "bin" / executable
    candidates.append(package_binary)

    for root in _candidate_repo_roots():
        candidates.extend(
            [
                root / "rust" / "target" / "release" / executable,
                root / "rust" / "target" / "debug" / executable,
                root / "rust" / "target" / "x86_64-unknown-linux-gnu" / "release" / executable,
            ]
        )

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _candidate_repo_roots() -> list[Path]:
    roots = [Path(__file__).resolve().parents[3], Path.cwd()]
    roots.extend(Path.cwd().parents)

    seen: set[Path] = set()
    unique_roots: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_roots.append(resolved)
    return unique_roots


__all__ = ["tui"]
