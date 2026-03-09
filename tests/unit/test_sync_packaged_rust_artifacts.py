from __future__ import annotations

import os
from pathlib import Path

from scripts.sync_packaged_rust_artifacts import (
    ArtifactSpec,
    SyncLayout,
    sync_packaged_rust_artifacts,
    sync_reason,
)


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _set_mtime(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def _layout(tmp_path: Path) -> SyncLayout:
    root_dir = tmp_path
    rust_manifest = root_dir / "rust" / "Cargo.toml"
    release_dir = root_dir / "rust" / "target" / "release"
    package_bin_dir = root_dir / "roar" / "bin"
    return SyncLayout(
        root_dir=root_dir,
        rust_manifest=rust_manifest,
        release_dir=release_dir,
        package_bin_dir=package_bin_dir,
        artifacts=(
            ArtifactSpec(
                package_name="roar-tracer",
                source_paths=(
                    rust_manifest,
                    root_dir / "rust" / "Cargo.lock",
                    root_dir / "rust" / "tracers" / "ptrace",
                ),
                binary_names=("roar-tracer",),
            ),
            ArtifactSpec(
                package_name="roar-tracer-preload",
                source_paths=(
                    rust_manifest,
                    root_dir / "rust" / "Cargo.lock",
                    root_dir / "rust" / "tracers" / "preload",
                ),
                binary_names=("roar-tracer-preload",),
                library_names=("libroar_tracer_preload.so",),
            ),
        ),
    )


def test_sync_reason_detects_non_preload_tracer_staleness(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    cargo_lock = layout.root_dir / "rust" / "Cargo.lock"
    ptrace_source = layout.root_dir / "rust" / "tracers" / "ptrace" / "src" / "main.rs"
    release_tracer = layout.release_dir / "roar-tracer"
    package_tracer = layout.package_bin_dir / "roar-tracer"

    _write_file(layout.rust_manifest, "[workspace]\n")
    _write_file(cargo_lock, "")
    _write_file(ptrace_source, "ptrace source\n")
    _write_file(release_tracer, "release-tracer\n")
    _write_file(package_tracer, "release-tracer\n")
    _write_file(layout.release_dir / "roar-tracer-preload", "release-preload\n")
    _write_file(layout.release_dir / "libroar_tracer_preload.so", "release-library\n")
    _write_file(layout.package_bin_dir / "roar-tracer-preload", "release-preload\n")
    _write_file(layout.package_bin_dir / "libroar_tracer_preload.so", "release-library\n")

    _set_mtime(layout.rust_manifest, 50.0)
    _set_mtime(cargo_lock, 50.0)
    _set_mtime(release_tracer, 300.0)
    _set_mtime(package_tracer, 100.0)
    _set_mtime(ptrace_source, 200.0)

    assert sync_reason(layout) == "packaged roar-tracer is older than its sources"


def test_sync_reason_detects_preload_library_mismatch(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_file(layout.rust_manifest, "[workspace]\n")
    _write_file(layout.root_dir / "rust" / "Cargo.lock", "")
    _write_file(layout.root_dir / "rust" / "tracers" / "ptrace" / "src" / "main.rs", "ptrace source\n")
    _write_file(layout.root_dir / "rust" / "tracers" / "preload" / "src" / "lib.rs", "preload source\n")
    _write_file(layout.release_dir / "roar-tracer", "release-tracer\n")
    _write_file(layout.package_bin_dir / "roar-tracer", "release-tracer\n")
    _write_file(layout.release_dir / "roar-tracer-preload", "release-preload\n")
    _write_file(layout.release_dir / "libroar_tracer_preload.so", "release-library\n")
    _write_file(layout.package_bin_dir / "roar-tracer-preload", "release-preload\n")
    _write_file(layout.package_bin_dir / "libroar_tracer_preload.so", "different-library\n")

    assert sync_reason(layout) == "packaged library for roar-tracer-preload differs from release artifact"


def test_sync_packaged_rust_artifacts_copies_release_outputs(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_file(layout.rust_manifest, "[workspace]\n")
    _write_file(layout.root_dir / "rust" / "Cargo.lock", "")
    _write_file(layout.root_dir / "rust" / "tracers" / "ptrace" / "src" / "main.rs", "ptrace source\n")
    _write_file(layout.root_dir / "rust" / "tracers" / "preload" / "src" / "lib.rs", "preload source\n")
    _write_file(layout.release_dir / "roar-tracer", "release-tracer\n")
    _write_file(layout.release_dir / "roar-tracer-preload", "release-preload\n")
    _write_file(layout.release_dir / "libroar_tracer_preload.so", "release-library\n")

    sync_packaged_rust_artifacts(layout)

    assert (layout.package_bin_dir / "roar-tracer").read_text(encoding="utf-8") == "release-tracer\n"
    assert (layout.package_bin_dir / "roar-tracer-preload").read_text(encoding="utf-8") == "release-preload\n"
    assert (layout.package_bin_dir / "libroar_tracer_preload.so").read_text(encoding="utf-8") == "release-library\n"
