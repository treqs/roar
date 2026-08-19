"""Verify roar wheel bundles native hash extension and required Rust binaries."""

from __future__ import annotations

import glob
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

# ELF e_machine values for the architectures we ship.
_ELF_MACHINE_X86_64 = 0x3E
_ELF_MACHINE_AARCH64 = 0xB7
_ELF_MACHINE_NAMES = {_ELF_MACHINE_X86_64: "x86_64", _ELF_MACHINE_AARCH64: "aarch64"}


def main() -> None:
    wheel_glob = os.environ.get("ROAR_WHEEL_GLOB", "dist/*.whl")
    wheels = sorted(glob.glob(wheel_glob))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one wheel matching {wheel_glob!r}, found: {wheels}")

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    platform = os.environ.get("ROAR_WHEEL_PLATFORM", "linux")
    expected_arch = _expected_arch_from_wheel(wheel)

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

    native_extensions = {
        name
        for name in names
        if name.startswith("roar/_hash_native")
        and (name.endswith(".so") or name.endswith(".pyd") or name.endswith(".dylib"))
    }
    has_native = bool(native_extensions)
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
        linux_elf_members = required_bins | native_extensions
        _verify_linux_glibc_floor(wheel, names, linux_elf_members)
        if expected_arch is not None:
            _verify_linux_bin_arch(wheel, names, linux_elf_members, expected_arch)

    print(f"Verified wheel contents: {wheel}")


def _expected_arch_from_wheel(wheel: str) -> int | None:
    """Pull the expected ELF e_machine out of the wheel's platform tag.

    Wheel filenames look like
    `roar_cli-0.2.11-cp312-cp312-manylinux_2_17_x86_64.whl`. We only check
    archs we actually ship; macOS wheels return None and are skipped.
    """
    name = os.path.basename(wheel)
    if "x86_64" in name and "linux" in name:
        return _ELF_MACHINE_X86_64
    if "aarch64" in name:
        return _ELF_MACHINE_AARCH64
    return None


def _verify_linux_bin_arch(
    wheel: str,
    names: set[str],
    required_bins: set[str],
    expected_machine: int,
) -> None:
    expected_name = _ELF_MACHINE_NAMES[expected_machine]
    members = sorted(required_bins) + sorted(
        name
        for name in names
        if name.startswith("roar/bin/libroar_tracer_preload")
        or name.startswith("roar/bin/libroar-tracer-preload")
    )

    with tempfile.TemporaryDirectory(prefix="roar-wheel-arch-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(wheel) as zf:
            for member in members:
                extracted = Path(zf.extract(member, tmp_path))
                machine = _elf_machine(extracted)
                if machine is None:
                    raise SystemExit(f"{member}: not an ELF file")
                if machine != expected_machine:
                    found = _ELF_MACHINE_NAMES.get(machine, hex(machine))
                    raise SystemExit(
                        f"{member}: e_machine={found} but wheel platform tag "
                        f"requires {expected_name}"
                    )

                # The bundled binaries must be executable; the sdist→wheel
                # path historically dropped exec bits and silently shipped
                # 0644 ELFs that pip could install but couldn't run.
                mode = (zf.getinfo(member).external_attr >> 16) & 0o7777
                if mode and not (mode & 0o111) and not member.endswith(".so"):
                    raise SystemExit(
                        f"{member}: missing executable bit in wheel (mode={oct(mode)})"
                    )


def _elf_machine(path: Path) -> int | None:
    with path.open("rb") as f:
        head = f.read(20)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return None
    # ELF e_machine is at offset 0x12 (18), 2 bytes, byte order from EI_DATA.
    little_endian = head[5] == 1
    fmt = "<H" if little_endian else ">H"
    return struct.unpack(fmt, head[18:20])[0]


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

    versions = [
        _parse_glibc_version(match.group(1))
        for match in re.finditer(r"GLIBC_(\d+\.\d+)", result.stdout)
    ]
    return max(versions) if versions else None


def _parse_glibc_version(raw: str) -> tuple[int, int]:
    major, minor = raw.split(".", 1)
    return int(major), int(minor)


if __name__ == "__main__":
    main()
