#!/usr/bin/env bash
set -euo pipefail

# Builds the roar-runtime OCI image (init-container staging + webhook base).
#
# Usage:
#   bash scripts/build_runtime_image.sh [TAG]     # default: roar-runtime:dev
#
# Requires a packaged wheel in dist/ (scripts/build_wheel_with_bins.sh).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TAG="${1:-roar-runtime:dev}"

shopt -s nullglob
wheels=("$ROOT_DIR"/dist/roar_cli-*.whl)
shopt -u nullglob
if ((${#wheels[@]} == 0)); then
  echo "error: no roar_cli wheel in $ROOT_DIR/dist" >&2
  echo "hint: build one first: bash scripts/build_wheel_with_bins.sh" >&2
  exit 1
fi

docker build -f "$ROOT_DIR/deploy/roar-runtime/Dockerfile" -t "$TAG" "$ROOT_DIR"
echo "✓ Built $TAG"
