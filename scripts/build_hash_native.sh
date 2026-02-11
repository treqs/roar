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
  echo "python is required to resolve EXT_SUFFIX for the native module." >&2
  exit 1
fi

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo is required to build the native hashing module." >&2
  exit 1
fi

cargo build --release --manifest-path rust/Cargo.toml -p artifact-hash-py

EXT_SUFFIX="$(
python - <<'PY'
import sysconfig
print(sysconfig.get_config_var("EXT_SUFFIX") or ".so")
PY
)"
SOURCE_LIB="rust/target/release/libartifact_hash_py.so"
TARGET_LIB="roar/_hash_native${EXT_SUFFIX}"

if [[ ! -f "$SOURCE_LIB" ]]; then
  echo "Expected built library not found: $SOURCE_LIB" >&2
  exit 1
fi

rm -f roar/_hash_native*.so roar/_hash_native*.pyd roar/_hash_native*.dylib
cp "$SOURCE_LIB" "$TARGET_LIB"
echo "Installed native module: $TARGET_LIB"
