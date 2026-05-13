#!/usr/bin/env bash
# Install roar from source for local development.
#
# `pip install -e .` (and `uv pip install -e .`) builds the
# `artifact-hash-py` pyo3 extension via maturin and copies it into the
# install — but it does NOT build the Rust tracer binaries
# (`roar-tracer*`, `roard`, `roar-proxy`), which are separate cargo
# packages outside the maturin manifest. The PyPI wheel ships the
# tracers in `roar/bin/`, so a wheel install just works; an editable
# source install ends up silently missing them and `roar run` fails
# with "No tracer binary found" the first time you try it.
#
# This script does the four steps a fresh contributor needs:
#   1. Install the Python package (editable, with dev extras)
#   2. Build the platform-appropriate Rust tracer crates
#   3. Stage the built binaries into `roar/bin/` so the editable install
#      finds them at runtime
#   4. Smoke-test with `roar tracer`
#
# Re-run safely; the pip install is idempotent and cargo is incremental.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUST_MANIFEST="$ROOT_DIR/rust/Cargo.toml"
RELEASE_DIR="$ROOT_DIR/rust/target/release"
BIN_DIR="$ROOT_DIR/roar/bin"

cd "$ROOT_DIR"

step() { printf '\n▶ %s\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
err() { printf '  ✗ %s\n' "$*" >&2; }

# 1. Python install ----------------------------------------------------------

step "Installing Python package (editable, with dev extras)"
if command -v uv >/dev/null 2>&1; then
  uv pip install -e ".[dev]"
  ok "uv pip install -e .[dev]"
elif command -v pip >/dev/null 2>&1; then
  pip install -e ".[dev]"
  ok "pip install -e .[dev]"
else
  err "neither uv nor pip is on PATH"
  exit 1
fi

# 2. Rust toolchain check ---------------------------------------------------

if ! command -v cargo >/dev/null 2>&1; then
  err "cargo not found — install via https://rustup.rs/ then re-run this script"
  exit 1
fi

# 3. Per-platform tracer set -------------------------------------------------

case "$(uname -s)" in
  Linux)
    # Linux ships all three tracer flavors plus roard + the proxy.
    PACKAGES=(roar-tracer roar-tracer-preload roar-tracer-ebpf roar-proxy)
    BINARIES=(roar-tracer roar-tracer-preload roar-tracer-ebpf roard roar-proxy)
    NEED_BPF_LINKER=1
    ;;
  Darwin)
    # macOS supports preload + proxy only.
    PACKAGES=(roar-tracer-preload roar-proxy)
    BINARIES=(roar-tracer-preload roar-proxy)
    NEED_BPF_LINKER=0
    ;;
  *)
    err "unsupported platform $(uname -s); roar's tracer builds are Linux/macOS only"
    exit 1
    ;;
esac

# 4. eBPF tooling preflight (Linux) -----------------------------------------

if [[ "$NEED_BPF_LINKER" == "1" ]]; then
  if ! command -v bpf-linker >/dev/null 2>&1; then
    warn "bpf-linker not found — needed by roar-tracer-ebpf"
    warn "  install via:  cargo install bpf-linker"
    warn "  also: rustup install nightly && rustup component add rust-src --toolchain nightly"
    warn "skipping eBPF tracer build (other tracers will still work)"
    PACKAGES=("${PACKAGES[@]/roar-tracer-ebpf/}")
    BINARIES=("${BINARIES[@]/roar-tracer-ebpf/}")
    BINARIES=("${BINARIES[@]/roard/}")
  fi
fi

# 5. Build the tracer binaries ----------------------------------------------

step "Building tracer crates: ${PACKAGES[*]}"
build_args=()
for pkg in "${PACKAGES[@]}"; do
  [[ -n "$pkg" ]] && build_args+=(-p "$pkg")
done
cargo build --release --manifest-path "$RUST_MANIFEST" "${build_args[@]}"
ok "cargo build complete"

# 6. Stage binaries + preload library into roar/bin/ ------------------------

step "Staging binaries into $BIN_DIR"
mkdir -p "$BIN_DIR"

for binary in "${BINARIES[@]}"; do
  [[ -z "$binary" ]] && continue
  src="$RELEASE_DIR/$binary"
  if [[ ! -f "$src" ]]; then
    warn "expected binary not produced: $binary (skipping)"
    continue
  fi
  install -m 0755 "$src" "$BIN_DIR/$binary"
  ok "$binary"
done

# Preload interposer .so (Linux) / .dylib (macOS) — naming may use either
# underscores or dashes depending on cc crate version. Iterate concrete
# candidates and skip ones that don't exist.
for lib in \
  "$RELEASE_DIR"/libroar_tracer_preload.so \
  "$RELEASE_DIR"/libroar-tracer-preload.so \
  "$RELEASE_DIR"/libroar_tracer_preload.dylib \
  "$RELEASE_DIR"/libroar-tracer-preload.dylib
do
  [[ -f "$lib" ]] || continue
  install -m 0755 "$lib" "$BIN_DIR/$(basename "$lib")"
  ok "$(basename "$lib")"
done

# 7. Smoke test --------------------------------------------------------------

step "Smoke-testing the install"
if ! command -v roar >/dev/null 2>&1; then
  warn "'roar' not on PATH — activate your venv or rehash your shell"
else
  roar tracer || warn "roar tracer reported issues — see output above"
  ok "roar CLI is on PATH"
fi

printf '\n✓ Dev install complete. Try: roar run python your_script.py\n'
