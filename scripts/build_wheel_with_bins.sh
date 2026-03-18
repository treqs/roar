#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${1:-$ROOT_DIR/dist}"
RUST_MANIFEST="$ROOT_DIR/rust/Cargo.toml"
BIN_DIR="$ROOT_DIR/roar/bin"
HOST_RELEASE_DIR="$ROOT_DIR/rust/target/release"
LINUX_PORTABLE_RELEASE_DIR="$ROOT_DIR/rust/target/x86_64-unknown-linux-gnu/release"
LINUX_GLIBC_FLOOR="2.17"

mkdir -p "$OUT_DIR" "$BIN_DIR"

echo "▶ Syncing packaged Rust artifacts into roar/bin..."
python3 "$ROOT_DIR/scripts/sync_packaged_rust_artifacts.py"

declare -a packages_to_build=()
declare -A binaries_to_sync=()
sync_preload_lib=0
build_output_dir="$HOST_RELEASE_DIR"

queue_package() {
  local package="$1"
  local existing
  for existing in "${packages_to_build[@]:-}"; do
    if [[ "$existing" == "$package" ]]; then
      return
    fi
  done
  packages_to_build+=("$package")
}

max_glibc_version() {
  local path="$1"
  if [[ ! -f "$path" ]] || ! command -v objdump >/dev/null 2>&1; then
    return 0
  fi

  objdump -p "$path" \
    | awk '/GLIBC_/ {print $NF}' \
    | sed -E 's/.*GLIBC_([0-9]+\.[0-9]+).*/\1/' \
    | sort -V \
    | tail -n 1
}

needs_portable_linux_rebuild() {
  local path="$1"
  if [[ "$(uname -s)" != "Linux" ]] || [[ ! -f "$path" ]]; then
    return 1
  fi

  local max_glibc
  max_glibc="$(max_glibc_version "$path")"
  if [[ -z "$max_glibc" ]]; then
    return 1
  fi

  if [[ "$(printf '%s\n%s\n' "$LINUX_GLIBC_FLOOR" "$max_glibc" | sort -V | tail -n 1)" != "$LINUX_GLIBC_FLOOR" ]]; then
    echo "▶ Rebuilding $(basename "$path") for glibc portability (found GLIBC_${max_glibc}, need <= GLIBC_${LINUX_GLIBC_FLOOR})"
    return 0
  fi

  return 1
}

ensure_binary() {
  local package="$1"
  local binary="$2"
  local dst="$BIN_DIR/$binary"
  if [[ ! -f "$dst" ]] || needs_portable_linux_rebuild "$dst"; then
    queue_package "$package"
    binaries_to_sync["$binary"]=1
  fi
}

ensure_preload_library() {
  local found_any=0
  local needs_rebuild=0
  local library
  for library in \
    libroar_tracer_preload.so \
    libroar-tracer-preload.so \
    libroar_tracer_preload.dylib \
    libroar-tracer-preload.dylib
  do
    local dst="$BIN_DIR/$library"
    if [[ -f "$dst" ]]; then
      found_any=1
      if needs_portable_linux_rebuild "$dst"; then
        needs_rebuild=1
      fi
    fi
  done

  if [[ "$found_any" -eq 0 ]] || [[ "$needs_rebuild" -eq 1 ]]; then
    queue_package "roar-tracer-preload"
    sync_preload_lib=1
  fi
}

setup_portable_linux_builder() {
  if ! cargo zigbuild --help >/dev/null 2>&1; then
    echo "▶ Installing cargo-zigbuild..."
    cargo install cargo-zigbuild
  fi

  if ! command -v python-zig >/dev/null 2>&1; then
    echo "▶ Installing ziglang tool..."
    uv tool install ziglang
  fi

  export CARGO_ZIGBUILD_ZIG_PATH="${CARGO_ZIGBUILD_ZIG_PATH:-$(command -v python-zig)}"
  build_output_dir="$LINUX_PORTABLE_RELEASE_DIR"
}

resolve_built_artifact() {
  local name="$1"
  local candidate
  for candidate in "$build_output_dir/$name" "$HOST_RELEASE_DIR/$name"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

build_python_wheel() {
  if command -v uv >/dev/null 2>&1; then
    echo "▶ Building wheel with uv..."
    uv build --wheel --out-dir "$OUT_DIR"
    return
  fi

  local python_candidate
  for python_candidate in "${PYTHON:-}" python3 python; do
    if [[ -z "$python_candidate" ]]; then
      continue
    fi
    if [[ "$python_candidate" == */* ]]; then
      if [[ ! -x "$python_candidate" ]]; then
        continue
      fi
    elif ! command -v "$python_candidate" >/dev/null 2>&1; then
      continue
    fi
    if command -v maturin >/dev/null 2>&1; then
      echo "▶ Building wheel with maturin..."
      (
        cd "$ROOT_DIR"
        maturin build \
          --release \
          --manifest-path rust/crates/artifact-hash-py/Cargo.toml \
          --interpreter "$python_candidate" \
          --out "$OUT_DIR"
      )
      return
    fi
    if "$python_candidate" -m maturin --help >/dev/null 2>&1; then
      echo "▶ Building wheel with $python_candidate -m maturin..."
      (
        cd "$ROOT_DIR"
        "$python_candidate" -m maturin build \
          --release \
          --manifest-path rust/crates/artifact-hash-py/Cargo.toml \
          --interpreter "$python_candidate" \
          --out "$OUT_DIR"
      )
      return
    fi
  done

  echo "error: wheel build requires either uv or maturin available" >&2
  exit 1
}

ensure_binary "roar-proxy" "roar-proxy"
ensure_binary "roar-tracer" "roar-tracer"
ensure_binary "roar-tracer-ebpf" "roar-tracer-ebpf"
ensure_binary "roar-tracer-ebpf" "roard"
ensure_binary "roar-tracer-preload" "roar-tracer-preload"
ensure_preload_library

if ((${#packages_to_build[@]} > 0)); then
  echo "▶ Building packaged Rust binaries: ${packages_to_build[*]}"
  if [[ "$(uname -s)" == "Linux" ]]; then
    setup_portable_linux_builder
    build_cmd=(
      cargo zigbuild
      --release
      --manifest-path "$RUST_MANIFEST"
      --target x86_64-unknown-linux-gnu.2.17
    )
  else
    build_cmd=(cargo build --release --manifest-path "$RUST_MANIFEST")
  fi

  for package in "${packages_to_build[@]}"; do
    build_cmd+=(-p "$package")
  done
  "${build_cmd[@]}"
else
  echo "▶ Packaged Rust binaries already present in roar/bin"
fi

echo "▶ Syncing packaged binaries into roar/bin..."
for binary in roar-proxy roar-tracer roar-tracer-ebpf roar-tracer-preload roard; do
  if [[ -z "${binaries_to_sync[$binary]:-}" ]]; then
    continue
  fi

  src="$(resolve_built_artifact "$binary")" || {
    echo "error: expected binary not found for packaging: $binary" >&2
    exit 1
  }
  install -m 0755 "$src" "$BIN_DIR/$binary"
done

if [[ "$sync_preload_lib" -eq 1 ]]; then
  copied_lib=0
  for library in \
    libroar_tracer_preload.so \
    libroar-tracer-preload.so \
    libroar_tracer_preload.dylib \
    libroar-tracer-preload.dylib
  do
    src="$(resolve_built_artifact "$library" || true)"
    if [[ -n "$src" ]]; then
      install -m 0755 "$src" "$BIN_DIR/$library"
      copied_lib=1
    fi
  done

  if [[ "$copied_lib" -ne 1 ]]; then
    echo "error: no preload interposer library available for packaging" >&2
    exit 1
  fi
fi

echo "▶ Building roar wheel into $OUT_DIR..."
build_python_wheel

echo "▶ Verifying wheel contents..."
(
  cd "$ROOT_DIR"
  ROAR_WHEEL_GLOB="$OUT_DIR/roar_cli-*.whl" python3 scripts/ci/verify_wheel_contents.py
)

echo "✓ Wheel build complete"
