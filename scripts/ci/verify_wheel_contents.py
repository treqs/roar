"""Verify roar wheel bundles native hash extension and required Rust binaries."""

from __future__ import annotations

import glob
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile


def main() -> None:
    wheel_glob = os.environ.get("ROAR_WHEEL_GLOB", "dist/*.whl")
    wheels = sorted(glob.glob(wheel_glob))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one wheel matching {wheel_glob!r}, found: {wheels}")

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    platform = os.environ.get("ROAR_WHEEL_PLATFORM", "linux")

    if platform == "macos":
        required_bins = {
            "roar/bin/roar-proxy",
            "roar/bin/roar-tracer-preload",
        }
        linux_only_bins = {
            "roar/bin/roar-tracer",
            "roar/bin/roar-tracer-ebpf",
            "roar/bin/roard",
        }
        unexpected = sorted(path for path in linux_only_bins if path in names)
        if unexpected:
            raise SystemExit(f"Linux-only binaries found in macOS wheel: {unexpected}")
    else:
        required_bins = {
            "roar/bin/roar-tracer",
            "roar/bin/roar-proxy",
            "roar/bin/roar-tracer-ebpf",
            "roar/bin/roard",
            "roar/bin/roar-tracer-preload",
        }

    missing_bins = sorted(path for path in required_bins if path not in names)
    if missing_bins:
        raise SystemExit(f"Missing binaries in wheel: {missing_bins}")

    has_native = any(
        name.startswith("roar/_hash_native")
        and (name.endswith(".so") or name.endswith(".pyd") or name.endswith(".dylib"))
        for name in names
    )
    if not has_native:
        raise SystemExit("Missing native hash extension in wheel (roar/_hash_native*)")

    has_preload_lib = any(
        name.startswith("roar/bin/libroar_tracer_preload")
        or name.startswith("roar/bin/libroar-tracer-preload")
        for name in names
    )
    if not has_preload_lib:
        raise SystemExit("Missing preload interposer library in wheel (roar/bin/libroar*_preload*)")

    if platform == "linux":
        _verify_linux_glibc_floor(wheel, names, required_bins)

    print(f"Verified wheel contents: {wheel}")


def _verify_linux_glibc_floor(wheel: str, names: set[str], required_bins: set[str]) -> None:
    max_allowed = _parse_glibc_version(os.environ.get("ROAR_WHEEL_MAX_GLIBC", "2.17"))
    members = sorted(required_bins) + sorted(
        name
        for name in names
        if name.startswith("roar/bin/libroar_tracer_preload")
        or name.startswith("roar/bin/libroar-tracer-preload")
    )

    with tempfile.TemporaryDirectory(prefix="roar-wheel-verify-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(wheel) as zf:
            for member in members:
                extracted = Path(zf.extract(member, tmp_path))
                max_found = _max_glibc_version(extracted)
                if max_found is None:
                    continue
                if max_found > max_allowed:
                    raise SystemExit(
                        f"{member} requires GLIBC_{max_found[0]}.{max_found[1]} "
                        f"(max allowed GLIBC_{max_allowed[0]}.{max_allowed[1]})"
                    )


def _max_glibc_version(path: Path) -> tuple[int, int] | None:
    if not shutil.which("objdump"):
        raise SystemExit("objdump is required to verify Linux wheel portability")

    result = subprocess.run(
        ["objdump", "-p", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"objdump failed for {path}: {result.stderr.strip()}")

    versions = [_parse_glibc_version(match.group(1)) for match in re.finditer(r"GLIBC_(\d+\.\d+)", result.stdout)]
    return max(versions) if versions else None


def _parse_glibc_version(raw: str) -> tuple[int, int]:
    major, minor = raw.split(".", 1)
    return int(major), int(minor)


if __name__ == "__main__":
    main()
