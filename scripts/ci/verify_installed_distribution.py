"""Smoke-test an installed roar wheel as a runtime distribution."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _expected_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise SystemExit(f"Unsupported runner platform: {sys.platform}")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(msg)


def main() -> None:
    expected = os.environ.get("ROAR_WHEEL_PLATFORM")
    actual = _expected_platform()
    if expected:
        _assert(
            expected == actual,
            f"Runner/wheel platform mismatch: expected={expected}, actual={actual}",
        )

    import roar  # Imported after platform checks for clearer failures.

    module_path = Path(roar.__file__).resolve()
    normalized_module = str(module_path).replace("\\", "/")
    _assert(
        "/site-packages/" in normalized_module,
        f"Expected installed wheel import from site-packages, got: {module_path}",
    )

    bin_dir = module_path.parent / "bin"
    _assert(bin_dir.is_dir(), f"Missing packaged binary directory: {bin_dir}")

    required_bins = {"roar-proxy", "roar-tracer-preload"}
    if actual == "linux":
        required_bins.update({"roar-tracer", "roar-tracer-ebpf", "roard"})

    for binary in sorted(required_bins):
        path = bin_dir / binary
        _assert(path.exists(), f"Missing packaged binary: {path}")
        _assert(path.is_file(), f"Expected file for binary: {path}")
        _assert(os.access(path, os.X_OK), f"Packaged binary is not executable: {path}")

    if actual == "macos":
        linux_only = {"roar-tracer", "roar-tracer-ebpf", "roard"}
        unexpected = [name for name in sorted(linux_only) if (bin_dir / name).exists()]
        _assert(not unexpected, f"Linux-only binaries present in macOS wheel: {unexpected}")

    expected_lib_ext = ".dylib" if actual == "macos" else ".so"
    preload_libs = [
        p
        for p in bin_dir.iterdir()
        if p.is_file()
        and (
            p.name.startswith("libroar_tracer_preload") or p.name.startswith("libroar-tracer-preload")
        )
        and p.name.endswith(expected_lib_ext)
    ]
    _assert(
        bool(preload_libs),
        f"Missing preload interposer library with extension {expected_lib_ext} in {bin_dir}",
    )

    help_result = subprocess.run(
        ["roar", "--help"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    _assert(help_result.returncode == 0, "Installed `roar --help` smoke test failed")

    print(f"Verified installed roar distribution at {module_path.parent}")


if __name__ == "__main__":
    main()
