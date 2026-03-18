#!/usr/bin/env python3
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LINUX_PORTABLE_TARGET = "x86_64-unknown-linux-gnu.2.17"
LINUX_PORTABLE_TARGET_DIR = "x86_64-unknown-linux-gnu"


@dataclass(frozen=True)
class ArtifactSpec:
    package_name: str
    source_paths: tuple[Path, ...]
    binary_names: tuple[str, ...] = ()
    library_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncLayout:
    root_dir: Path
    rust_manifest: Path
    release_dir: Path
    package_bin_dir: Path
    artifacts: tuple[ArtifactSpec, ...]
    fallback_release_dirs: tuple[Path, ...] = ()
    portable_target: str | None = None


def _common_tracer_sources(root_dir: Path) -> tuple[Path, ...]:
    return (
        root_dir / "rust" / "Cargo.toml",
        root_dir / "rust" / "Cargo.lock",
        root_dir / "rust" / "crates" / "tracer-fd",
        root_dir / "rust" / "crates" / "tracer-runtime",
        root_dir / "rust" / "crates" / "tracer-schema",
    )


def _default_layout() -> SyncLayout:
    root_dir = Path(__file__).resolve().parents[1]
    library_suffix = ".dylib" if sys.platform == "darwin" else ".so"
    common_sources = _common_tracer_sources(root_dir)
    host_release_dir = root_dir / "rust" / "target" / "release"
    release_dir = host_release_dir
    fallback_release_dirs: tuple[Path, ...] = ()
    portable_target = None

    if sys.platform.startswith("linux"):
        release_dir = root_dir / "rust" / "target" / LINUX_PORTABLE_TARGET_DIR / "release"
        fallback_release_dirs = (host_release_dir,)
        portable_target = LINUX_PORTABLE_TARGET

    artifacts = [
        ArtifactSpec(
            package_name="roar-proxy",
            source_paths=(
                root_dir / "rust" / "Cargo.toml",
                root_dir / "rust" / "Cargo.lock",
                root_dir / "rust" / "services" / "proxy",
            ),
            binary_names=("roar-proxy",),
        ),
        ArtifactSpec(
            package_name="roar-tracer-preload",
            source_paths=(
                *common_sources,
                root_dir / "rust" / "tracers" / "preload",
            ),
            binary_names=("roar-tracer-preload",),
            library_names=(
                f"libroar_tracer_preload{library_suffix}",
                f"libroar-tracer-preload{library_suffix}",
            ),
        ),
    ]

    if sys.platform.startswith("linux"):
        artifacts.extend(
            [
                ArtifactSpec(
                    package_name="roar-tracer",
                    source_paths=(
                        *common_sources,
                        root_dir / "rust" / "tracers" / "ptrace",
                    ),
                    binary_names=("roar-tracer",),
                ),
                ArtifactSpec(
                    package_name="roar-tracer-ebpf",
                    source_paths=(
                        *common_sources,
                        root_dir / "rust" / "tracers" / "ebpf" / "common",
                        root_dir / "rust" / "tracers" / "ebpf" / "probe",
                        root_dir / "rust" / "tracers" / "ebpf" / "userspace",
                    ),
                    binary_names=("roar-tracer-ebpf", "roard"),
                ),
            ]
        )

    return SyncLayout(
        root_dir=root_dir,
        rust_manifest=root_dir / "rust" / "Cargo.toml",
        release_dir=release_dir,
        package_bin_dir=root_dir / "roar" / "bin",
        artifacts=tuple(artifacts),
        fallback_release_dirs=fallback_release_dirs,
        portable_target=portable_target,
    )


def _candidate_release_dirs(layout: SyncLayout) -> tuple[Path, ...]:
    return (layout.release_dir, *layout.fallback_release_dirs)


def _find_release_binary(layout: SyncLayout, binary_name: str) -> Path | None:
    for release_dir in _candidate_release_dirs(layout):
        candidate = release_dir / binary_name
        if candidate.exists():
            return candidate
    return None


def _find_release_library(layout: SyncLayout, names: tuple[str, ...]) -> Path | None:
    for release_dir in _candidate_release_dirs(layout):
        candidate = _first_existing_path(release_dir, names)
        if candidate is not None:
            return candidate
    return None


def _iter_source_files(paths: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for source_path in paths:
        if not source_path.exists():
            continue
        if source_path.is_file():
            files.append(source_path)
            continue
        files.extend(path for path in source_path.rglob("*") if path.is_file())
    return files


def _latest_mtime(paths: list[Path]) -> float:
    if not paths:
        return 0.0
    return max(path.stat().st_mtime for path in paths)


def _artifact_is_stale(path: Path, latest_source_mtime: float) -> bool:
    return not path.exists() or path.stat().st_mtime < latest_source_mtime


def _first_existing_path(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _sync_reason_for_path(
    *,
    release_path: Path | None,
    package_path: Path,
    latest_source_mtime: float,
    missing_release_reason: str,
    stale_release_reason: str,
    stale_package_reason: str,
    differs_reason: str,
) -> str | None:
    if release_path is None:
        return missing_release_reason
    if _artifact_is_stale(release_path, latest_source_mtime):
        return stale_release_reason
    if _artifact_is_stale(package_path, latest_source_mtime):
        return stale_package_reason
    if not filecmp.cmp(release_path, package_path, shallow=False):
        return differs_reason
    return None


def sync_reason(layout: SyncLayout) -> str | None:
    for artifact in layout.artifacts:
        latest_source_mtime = _latest_mtime(_iter_source_files(artifact.source_paths))

        for binary_name in artifact.binary_names:
            reason = _sync_reason_for_path(
                release_path=_find_release_binary(layout, binary_name),
                package_path=layout.package_bin_dir / binary_name,
                latest_source_mtime=latest_source_mtime,
                missing_release_reason=f"release {binary_name} is missing",
                stale_release_reason=f"release {binary_name} is older than its sources",
                stale_package_reason=f"packaged {binary_name} is older than its sources",
                differs_reason=f"packaged {binary_name} differs from release artifact",
            )
            if reason is not None:
                return reason

        if artifact.library_names:
            release_library = _find_release_library(layout, artifact.library_names)
            package_library = _first_existing_path(layout.package_bin_dir, artifact.library_names)
            if release_library is None:
                return f"release library for {artifact.package_name} is missing"
            package_target = package_library or layout.package_bin_dir / release_library.name
            reason = _sync_reason_for_path(
                release_path=release_library,
                package_path=package_target,
                latest_source_mtime=latest_source_mtime,
                missing_release_reason=f"release library for {artifact.package_name} is missing",
                stale_release_reason=f"release library for {artifact.package_name} is older than its sources",
                stale_package_reason=f"packaged library for {artifact.package_name} is older than its sources",
                differs_reason=f"packaged library for {artifact.package_name} differs from release artifact",
            )
            if reason is not None:
                return reason
    return None


def _packages_needing_build(layout: SyncLayout) -> list[str]:
    packages: list[str] = []
    for artifact in layout.artifacts:
        latest_source_mtime = _latest_mtime(_iter_source_files(artifact.source_paths))
        needs_build = False
        for binary_name in artifact.binary_names:
            release_binary = _find_release_binary(layout, binary_name)
            if release_binary is None or _artifact_is_stale(release_binary, latest_source_mtime):
                needs_build = True
                break
        if not needs_build and artifact.library_names:
            release_library = _find_release_library(layout, artifact.library_names)
            if release_library is None or _artifact_is_stale(release_library, latest_source_mtime):
                needs_build = True
        if needs_build and artifact.package_name not in packages:
            packages.append(artifact.package_name)
    return packages


def _build_release_artifacts(layout: SyncLayout, packages: list[str]) -> None:
    if not packages:
        return
    command = ["cargo"]
    env = os.environ.copy()
    if layout.portable_target:
        command.extend(
            [
                "zigbuild",
                "--release",
                "--manifest-path",
                str(layout.rust_manifest),
                "--target",
                layout.portable_target,
            ]
        )
        zig_path = shutil.which("python-zig") or shutil.which("zig")
        if zig_path:
            env.setdefault("CARGO_ZIGBUILD_ZIG_PATH", zig_path)
    else:
        command.extend(
            [
                "build",
                "--release",
                "--manifest-path",
                str(layout.rust_manifest),
            ]
        )
    for package in packages:
        command.extend(["-p", package])
    subprocess.run(command, check=True, cwd=layout.root_dir, env=env)


def _sync_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    dst.chmod(0o755)


def sync_packaged_rust_artifacts(layout: SyncLayout) -> None:
    _build_release_artifacts(layout, _packages_needing_build(layout))

    for artifact in layout.artifacts:
        for binary_name in artifact.binary_names:
            release_path = _find_release_binary(layout, binary_name)
            if release_path is None:
                raise SystemExit(f"release {binary_name} is missing after build")
            _sync_file(release_path, layout.package_bin_dir / binary_name)

        if artifact.library_names:
            release_library = _find_release_library(layout, artifact.library_names)
            if release_library is None:
                raise SystemExit(
                    f"release library for {artifact.package_name} is missing after build"
                )
            for library_name in artifact.library_names:
                candidate = layout.package_bin_dir / library_name
                if candidate.exists() and candidate.name != release_library.name:
                    candidate.unlink()
            _sync_file(release_library, layout.package_bin_dir / release_library.name)


# Backward-compatible alias while callers migrate from the preload-only name.
sync_packaged_preload = sync_packaged_rust_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the packaged Rust tracer/proxy artifacts in roar/bin with the Rust source.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any packaged Rust artifact needs to be rebuilt or resynced",
    )
    args = parser.parse_args()

    layout = _default_layout()
    reason = sync_reason(layout)
    if args.check:
        if reason is not None:
            raise SystemExit(reason)
        print("packaged Rust artifacts are up to date")
        return

    sync_packaged_rust_artifacts(layout)
    print("synced packaged Rust artifacts")


if __name__ == "__main__":
    main()
