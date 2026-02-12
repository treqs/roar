#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer project virtualenv when present (local dev), but support CI/global Python too.
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v python >/dev/null 2>&1; then
  echo "python is required to build/install the native module." >&2
  exit 1
fi

if ! command -v maturin >/dev/null 2>&1; then
  echo "maturin is required to build the native hashing module." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

maturin build \
  --release \
  --manifest-path rust/crates/artifact-hash-py/Cargo.toml \
  --interpreter "$(command -v python)" \
  --out "$TMP_DIR"

WHEEL_PATH="$(ls "$TMP_DIR"/*.whl | head -n1)"
if [[ -z "$WHEEL_PATH" ]]; then
  echo "maturin did not produce a wheel in $TMP_DIR" >&2
  exit 1
fi

python - "$WHEEL_PATH" <<'PY'
import shutil
import sys
import zipfile
from pathlib import Path

wheel_path = Path(sys.argv[1])
roar_dir = Path("roar")
roar_dir.mkdir(parents=True, exist_ok=True)

for pattern in ("_hash_native*.so", "_hash_native*.pyd", "_hash_native*.dylib"):
    for existing in roar_dir.glob(pattern):
        existing.unlink()

with zipfile.ZipFile(wheel_path) as zf:
    native_member = next(
        (
            name
            for name in zf.namelist()
            if name.startswith("roar/_hash_native")
            and name.rsplit("/", 1)[-1].endswith((".so", ".pyd", ".dylib"))
        ),
        None,
    )
    if native_member is None:
        raise SystemExit(f"Native module not found in wheel: {wheel_path}")

    target = roar_dir / Path(native_member).name
    with zf.open(native_member) as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    print(f"Installed native module: {target}")
PY
