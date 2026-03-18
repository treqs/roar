#!/usr/bin/env python3
"""Reusable profiling harness for representative roar workflows."""

from __future__ import annotations

import argparse
import json
import os
import pstats
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "tests" / "benchmarks" / "results"
PROFILE_ROOT = RESULTS_DIR / "profiles"
INJECT_DIR = REPO_ROOT / "roar" / "execution" / "runtime" / "inject"
DEFAULT_SCENARIOS = (
    "cli_help",
    "cli_run_simple",
    "cli_status_active",
    "cli_show_session",
    "cli_register_dry_run",
    "cli_put_dry_run",
    "startup_wrap",
)
IMPORTTIME_RE = re.compile(
    r"^import time:\s+(?P<self_us>\d+)\s+\|\s+(?P<cum_us>\d+)\s+\|\s+(?P<module>.+?)\s*$"
)


@dataclass(frozen=True)
class Hotspot:
    label: str
    primitive_calls: int
    total_calls: int
    cumulative_ms: float
    internal_ms: float


@dataclass(frozen=True)
class ImportHotspot:
    module: str
    self_ms: float
    cumulative_ms: float


@dataclass(frozen=True)
class CommandProfileResult:
    name: str
    kind: Literal["cli"]
    command: list[str]
    iterations: int
    mean_ms: float
    median_ms: float
    min_ms: float
    max_ms: float
    stdev_ms: float
    profile_path: str
    stdout_path: str
    stderr_path: str
    top_cumulative: list[Hotspot]
    top_internal: list[Hotspot]


@dataclass(frozen=True)
class StartupProfileResult:
    name: str
    kind: Literal["startup"]
    iterations: int
    baseline_mean_ms: float
    wrapped_mean_ms: float
    wrapped_with_log_mean_ms: float
    import_overhead_ms: float
    atexit_overhead_ms: float
    total_overhead_ms: float
    importtime_stderr_path: str
    top_imports: list[ImportHotspot]


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_commit() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    return "unknown"


def _slug_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _summary_stats(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        raise ValueError("No timing samples were collected")
    return {
        "mean_ms": statistics.fmean(samples_ms),
        "median_ms": statistics.median(samples_ms),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
        "stdev_ms": statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0,
    }


def _format_label(filename: str, line_no: int, func_name: str) -> str:
    try:
        path = Path(filename)
        rel = path.relative_to(REPO_ROOT)
        display = str(rel)
    except ValueError:
        display = filename
    return f"{display}:{line_no}::{func_name}"


def _summarize_profile(profile_path: Path, limit: int) -> tuple[list[Hotspot], list[Hotspot]]:
    stats = pstats.Stats(str(profile_path))
    entries: list[Hotspot] = []
    for func, stat in stats.stats.items():
        primitive_calls, total_calls, internal_s, cumulative_s, _callers = stat
        filename, line_no, func_name = func
        entries.append(
            Hotspot(
                label=_format_label(filename, line_no, func_name),
                primitive_calls=int(primitive_calls),
                total_calls=int(total_calls),
                cumulative_ms=cumulative_s * 1000.0,
                internal_ms=internal_s * 1000.0,
            )
        )

    top_cumulative = sorted(
        entries,
        key=lambda item: (item.cumulative_ms, item.internal_ms),
        reverse=True,
    )[:limit]
    top_internal = sorted(
        entries,
        key=lambda item: (item.internal_ms, item.cumulative_ms),
        reverse=True,
    )[:limit]
    return top_cumulative, top_internal


def _parse_importtime(stderr: str, limit: int) -> list[ImportHotspot]:
    hotspots: list[ImportHotspot] = []
    for line in stderr.splitlines():
        match = IMPORTTIME_RE.match(line)
        if not match:
            continue
        hotspots.append(
            ImportHotspot(
                module=match.group("module").strip(),
                self_ms=int(match.group("self_us")) / 1000.0,
                cumulative_ms=int(match.group("cum_us")) / 1000.0,
            )
        )
    return sorted(hotspots, key=lambda item: item.cumulative_ms, reverse=True)[:limit]


def _repo_env(*, inject_dir: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_entries: list[str] = []
    if inject_dir:
        pythonpath_entries.append(str(INJECT_DIR))
    pythonpath_entries.append(str(REPO_ROOT))
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        capture_output=capture_output,
        check=False,
        timeout=timeout,
    )


def _run_checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    proc = _run(command, cwd=cwd, env=env, timeout=timeout, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(command)}\n"
            f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
        )
    return proc


def _python_module_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "roar", *args]


def _git(repo: Path, *args: str) -> None:
    _run_checked(["git", *args], cwd=repo, env=_repo_env())


def _write_workspace_files(repo: Path) -> None:
    (repo / "input.txt").write_text("hello roar\n", encoding="utf-8")
    (repo / "transform.py").write_text(
        """
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _create_initialized_workspace(base_dir: Path) -> Path:
    repo = base_dir / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Roar Profiler")
    _git(repo, "config", "user.email", "profiler@example.com")
    _write_workspace_files(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial fixture")
    _run_checked(_python_module_command("init"), cwd=repo, env=_repo_env())
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initialize roar")
    return repo


def _seed_workspace(repo: Path) -> None:
    _run_checked(
        _python_module_command(
            "run",
            sys.executable,
            "transform.py",
            "input.txt",
            "output.txt",
        ),
        cwd=repo,
        env=_repo_env(),
        timeout=240,
    )


def _prepare_workspace(
    mode: Literal["none", "initialized", "seeded"],
) -> tuple[tempfile.TemporaryDirectory[str], Path | None]:
    temp_dir = tempfile.TemporaryDirectory(prefix="roar-profile-")
    repo: Path | None = None
    if mode != "none":
        repo = _create_initialized_workspace(Path(temp_dir.name))
        if mode == "seeded":
            _seed_workspace(repo)
    return temp_dir, repo


def _profile_command_scenario(
    *,
    name: str,
    args: list[str],
    workspace_mode: Literal["none", "initialized", "seeded"],
    iterations: int,
    profile_dir: Path,
    top: int,
) -> CommandProfileResult:
    samples_ms: list[float] = []
    profile_path = profile_dir / f"{name}.prof"
    stdout_path = profile_dir / f"{name}.stdout.txt"
    stderr_path = profile_dir / f"{name}.stderr.txt"

    for iteration in range(iterations):
        temp_dir, repo = _prepare_workspace(workspace_mode)
        try:
            cwd = repo if repo is not None else REPO_ROOT
            env = _repo_env()
            command = [sys.executable, "-m", "roar", *args]
            started = time.perf_counter()
            proc = _run(command, cwd=cwd, env=env, timeout=240, capture_output=True)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Scenario {name} failed on iteration {iteration + 1}: {proc.returncode}\n"
                    f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
                )
            samples_ms.append(elapsed_ms)
        finally:
            temp_dir.cleanup()

    temp_dir, repo = _prepare_workspace(workspace_mode)
    try:
        cwd = repo if repo is not None else REPO_ROOT
        env = _repo_env()
        profile_command = [
            sys.executable,
            "-m",
            "cProfile",
            "-o",
            str(profile_path),
            "-m",
            "roar",
            *args,
        ]
        profiled = _run(profile_command, cwd=cwd, env=env, timeout=240, capture_output=True)
        stdout_path.write_text(profiled.stdout, encoding="utf-8")
        stderr_path.write_text(profiled.stderr, encoding="utf-8")
        if profiled.returncode != 0:
            raise RuntimeError(
                f"Profiled scenario {name} failed: {profiled.returncode}\n"
                f"stdout:\n{profiled.stdout}\n\nstderr:\n{profiled.stderr}"
            )
    finally:
        temp_dir.cleanup()

    top_cumulative, top_internal = _summarize_profile(profile_path, top)
    summary = _summary_stats(samples_ms)
    return CommandProfileResult(
        name=name,
        kind="cli",
        command=_python_module_command(*args),
        iterations=iterations,
        profile_path=str(profile_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        top_cumulative=top_cumulative,
        top_internal=top_internal,
        **summary,
    )


def _measure_startup_command(env: dict[str, str], iterations: int) -> list[float]:
    samples_ms: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        proc = _run(
            [sys.executable, "-c", "pass"],
            cwd=REPO_ROOT,
            env=env,
            timeout=60,
            capture_output=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if proc.returncode != 0:
            raise RuntimeError("Startup probe failed")
        samples_ms.append(elapsed_ms)
    return samples_ms


def _profile_startup_scenario(
    *,
    iterations: int,
    profile_dir: Path,
    top: int,
) -> StartupProfileResult:
    baseline_env = _repo_env()
    baseline_env["ROAR_WRAP"] = "0"
    baseline_env.pop("ROAR_LOG_FILE", None)

    wrapped_env = _repo_env(inject_dir=True)
    wrapped_env["ROAR_WRAP"] = "1"
    wrapped_env.pop("ROAR_LOG_FILE", None)

    wrapped_with_log_env = _repo_env(inject_dir=True)
    wrapped_with_log_env["ROAR_WRAP"] = "1"
    wrapped_with_log_env["ROAR_LOG_FILE"] = str(profile_dir / "startup-log.json")

    baseline_samples = _measure_startup_command(baseline_env, iterations)
    wrapped_samples = _measure_startup_command(wrapped_env, iterations)
    wrapped_with_log_samples = _measure_startup_command(wrapped_with_log_env, iterations)

    importtime_path = profile_dir / "startup_wrap.importtime.txt"
    importtime_proc = _run(
        [sys.executable, "-X", "importtime", "-c", "pass"],
        cwd=REPO_ROOT,
        env=wrapped_env,
        timeout=60,
        capture_output=True,
    )
    importtime_path.write_text(importtime_proc.stderr, encoding="utf-8")
    if importtime_proc.returncode != 0:
        raise RuntimeError(
            f"Importtime startup probe failed:\nstdout:\n{importtime_proc.stdout}\n\nstderr:\n{importtime_proc.stderr}"
        )

    baseline_mean = statistics.fmean(baseline_samples)
    wrapped_mean = statistics.fmean(wrapped_samples)
    wrapped_with_log_mean = statistics.fmean(wrapped_with_log_samples)
    top_imports = _parse_importtime(importtime_proc.stderr, top)

    return StartupProfileResult(
        name="startup_wrap",
        kind="startup",
        iterations=iterations,
        baseline_mean_ms=baseline_mean,
        wrapped_mean_ms=wrapped_mean,
        wrapped_with_log_mean_ms=wrapped_with_log_mean,
        import_overhead_ms=wrapped_mean - baseline_mean,
        atexit_overhead_ms=wrapped_with_log_mean - wrapped_mean,
        total_overhead_ms=wrapped_with_log_mean - baseline_mean,
        importtime_stderr_path=str(importtime_path),
        top_imports=top_imports,
    )


def _scenario_specs() -> dict[
    str,
    tuple[
        Literal["cli", "startup"], list[str] | None, Literal["none", "initialized", "seeded"] | None
    ],
]:
    return {
        "cli_help": ("cli", ["--help"], "none"),
        "cli_run_simple": (
            "cli",
            ["run", sys.executable, "transform.py", "input.txt", "output.txt"],
            "initialized",
        ),
        "cli_status_active": ("cli", ["status"], "seeded"),
        "cli_show_session": ("cli", ["show", "--session"], "seeded"),
        "cli_register_dry_run": ("cli", ["register", "--dry-run", "@1"], "seeded"),
        "cli_put_dry_run": (
            "cli",
            ["put", "@1", "s3://benchmark-bucket/profiles", "-m", "profile run", "--dry-run"],
            "seeded",
        ),
        "startup_wrap": ("startup", None, None),
    }


def _markdown_report(
    *,
    metadata: dict[str, Any],
    command_results: list[CommandProfileResult],
    startup_result: StartupProfileResult | None,
) -> str:
    lines = [
        "# Roar Profiling Report",
        "",
        f"- Timestamp: `{metadata['timestamp']}`",
        f"- Git commit: `{metadata['git_commit']}`",
        f"- Python: `{metadata['python_version']}`",
        "",
        "## CLI Scenarios",
        "",
    ]

    for result in command_results:
        lines.extend(
            [
                f"### `{result.name}`",
                "",
                f"- Mean wall time: `{result.mean_ms:.1f}ms`",
                f"- Median wall time: `{result.median_ms:.1f}ms`",
                f"- Std dev: `{result.stdev_ms:.1f}ms`",
                f"- Command: `{subprocess.list2cmdline(result.command)}`",
                f"- Profile: `{result.profile_path}`",
                "",
                "Top cumulative hotspots:",
            ]
        )
        for hotspot in result.top_cumulative[:5]:
            lines.append(
                f"- `{hotspot.label}`: cumulative `{hotspot.cumulative_ms:.1f}ms`, internal `{hotspot.internal_ms:.1f}ms`"
            )
        lines.append("")

    if startup_result is not None:
        lines.extend(
            [
                "## Startup Scenario",
                "",
                f"- Baseline: `{startup_result.baseline_mean_ms:.1f}ms`",
                f"- ROAR_WRAP=1: `{startup_result.wrapped_mean_ms:.1f}ms`",
                f"- ROAR_WRAP=1 + LOG_FILE: `{startup_result.wrapped_with_log_mean_ms:.1f}ms`",
                f"- Import overhead: `{startup_result.import_overhead_ms:.1f}ms`",
                f"- Atexit overhead: `{startup_result.atexit_overhead_ms:.1f}ms`",
                f"- Total overhead: `{startup_result.total_overhead_ms:.1f}ms`",
                "",
                "Top imports by cumulative time:",
            ]
        )
        for hotspot in startup_result.top_imports[:8]:
            lines.append(
                f"- `{hotspot.module}`: cumulative `{hotspot.cumulative_ms:.1f}ms`, self `{hotspot.self_ms:.1f}ms`"
            )
        lines.append("")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile representative roar CLI and runtime scenarios."
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_scenario_specs()),
        help="Run only the selected scenario(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Wall-time iterations per scenario (default: 3).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=12,
        help="Number of hotspots to keep per scenario (default: 12).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_DIR / "profile_suite_latest.json",
        help="JSON summary output path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scenario_names = args.scenario or list(DEFAULT_SCENARIOS)
    specs = _scenario_specs()

    run_dir = PROFILE_ROOT / _slug_timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)

    command_results: list[CommandProfileResult] = []
    startup_result: StartupProfileResult | None = None

    for name in scenario_names:
        kind, cli_args, workspace_mode = specs[name]
        print(f"[profile] running {name}...", flush=True)
        if kind == "cli":
            assert cli_args is not None
            assert workspace_mode is not None
            result = _profile_command_scenario(
                name=name,
                args=cli_args,
                workspace_mode=workspace_mode,
                iterations=args.iterations,
                profile_dir=run_dir,
                top=args.top,
            )
            command_results.append(result)
            print(
                f"  mean={result.mean_ms:.1f}ms median={result.median_ms:.1f}ms stdev={result.stdev_ms:.1f}ms",
                flush=True,
            )
        else:
            startup_result = _profile_startup_scenario(
                iterations=args.iterations,
                profile_dir=run_dir,
                top=args.top,
            )
            print(
                "  baseline="
                f"{startup_result.baseline_mean_ms:.1f}ms "
                f"wrap={startup_result.wrapped_mean_ms:.1f}ms "
                f"wrap+log={startup_result.wrapped_with_log_mean_ms:.1f}ms",
                flush=True,
            )

    metadata = {
        "timestamp": _now_iso_utc(),
        "git_commit": _git_commit(),
        "python_version": sys.version.split()[0],
        "iterations": args.iterations,
        "top": args.top,
        "profile_artifacts_dir": str(run_dir),
    }

    payload = {
        "metadata": metadata,
        "scenarios": [asdict(result) for result in command_results],
        "startup": asdict(startup_result) if startup_result is not None else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(
        _markdown_report(
            metadata=metadata,
            command_results=command_results,
            startup_result=startup_result,
        ),
        encoding="utf-8",
    )

    latest_profile_dir = PROFILE_ROOT / "latest"
    if latest_profile_dir.exists() or latest_profile_dir.is_symlink():
        if latest_profile_dir.is_symlink() or latest_profile_dir.is_file():
            latest_profile_dir.unlink()
        else:
            shutil.rmtree(latest_profile_dir)
    shutil.copytree(run_dir, latest_profile_dir)

    print(f"[profile] wrote JSON summary to {args.output}", flush=True)
    print(f"[profile] wrote Markdown summary to {markdown_path}", flush=True)
    print(f"[profile] stored profile artifacts in {run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
