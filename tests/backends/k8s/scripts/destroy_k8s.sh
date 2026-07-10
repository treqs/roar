#!/usr/bin/env bash
set -euo pipefail

# Tears down the roar k8s lineage e2e KIND cluster.

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_BIN="$HARNESS_DIR/.tools/bin"
CLUSTER_NAME="roar-k8s-e2e"

export PATH="$TOOLS_BIN:$PATH"

if ! command -v kind >/dev/null 2>&1; then
  echo "kind is not installed; nothing to destroy" >&2
  exit 0
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  kind delete cluster --name "$CLUSTER_NAME"
  echo "✓ Deleted KIND cluster $CLUSTER_NAME"
else
  echo "KIND cluster $CLUSTER_NAME does not exist"
fi
