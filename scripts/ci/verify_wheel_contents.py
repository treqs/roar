"""Verify roar wheel bundles native hash extension and required Rust binaries."""

from __future__ import annotations

import glob
import zipfile


def main() -> None:
    wheels = sorted(glob.glob("dist/*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one wheel, found: {wheels}")

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    required_bins = {
        "roar/bin/roar-tracer",
        "roar/bin/roar-proxy",
        "roar/bin/roar-tracer-ebpf",
        "roar/bin/roard",
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

    print(f"Verified wheel contents: {wheel}")


if __name__ == "__main__":
    main()
