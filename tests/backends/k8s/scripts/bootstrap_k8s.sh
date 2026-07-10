#!/usr/bin/env bash
set -euo pipefail

# Tier-1 KIND harness bootstrap for the roar k8s lineage e2e tests.
#
# What it does:
#   1. Ensures kind/kubectl are available (downloads pinned versions into
#      tests/backends/k8s/.tools/bin when missing from PATH).
#   2. Requires a packaged roar_cli wheel in <repo>/dist (built with
#      scripts/build_wheel_with_bins.sh) and mounts dist/ into cluster nodes.
#   3. Creates the KIND cluster (1 control plane + 2 workers).
#   4. Wires an in-cluster `glaas` Service/Endpoints to the host glaas-api so
#      pods use the cluster-visible URL (http://glaas:3001) while the host
#      keeps using the host-visible URL (http://localhost:3001).
#   5. Pre-pulls the workload image and preflights pod -> glaas reachability
#      with a probe pod, so infra failures are diagnosed here rather than
#      inside product tests.
#   6. Optionally deploys MinIO for S3 scenarios (--with-minio).
#   7. Optionally installs the JobSet controller (--with-jobset) for the
#      Tier-2 distributed operator e2e tests.
#
# Usage:
#   bash tests/backends/k8s/scripts/bootstrap_k8s.sh [--with-minio] [--with-jobset] [--skip-glaas]

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
TOOLS_BIN="$HARNESS_DIR/.tools/bin"
DIST_DIR="$REPO_ROOT/dist"

KIND_VERSION="v0.29.0"
KUBECTL_VERSION="v1.33.1"
CLUSTER_NAME="roar-k8s-e2e"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
NAMESPACE="roar-e2e"
WORKLOAD_IMAGE="docker.io/library/python:3.12-slim"
HOST_GLAAS_URL="${HOST_GLAAS_URL:-http://localhost:3001}"
CLUSTER_GLAAS_PORT=3001

JOBSET_VERSION="v0.12.0"

WITH_MINIO=0
WITH_JOBSET=0
SKIP_GLAAS=0
for arg in "$@"; do
  case "$arg" in
    --with-minio) WITH_MINIO=1 ;;
    --with-jobset) WITH_JOBSET=1 ;;
    --skip-glaas) SKIP_GLAAS=1 ;;
    *)
      echo "error: unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

case "$(uname -m)" in
  x86_64 | amd64) ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *)
    echo "error: unsupported host arch $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$TOOLS_BIN"
export PATH="$TOOLS_BIN:$PATH"

ensure_tool() {
  local name="$1"
  local url="$2"
  if command -v "$name" >/dev/null 2>&1; then
    return
  fi
  echo "▶ Downloading $name into $TOOLS_BIN"
  curl -fsSL -o "$TOOLS_BIN/$name" "$url"
  chmod +x "$TOOLS_BIN/$name"
}

ensure_tool kind "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
ensure_tool kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"

kubectl_ctx() {
  kubectl --context "$KUBE_CONTEXT" "$@"
}

echo "▶ Checking for a packaged roar_cli wheel in $DIST_DIR"
shopt -s nullglob
wheels=("$DIST_DIR"/roar_cli-*.whl)
shopt -u nullglob
if ((${#wheels[@]} == 0)); then
  echo "error: no roar_cli wheel in $DIST_DIR" >&2
  echo "hint: build one first: bash scripts/build_wheel_with_bins.sh" >&2
  exit 1
fi
if ! printf '%s\n' "${wheels[@]}" | grep -Eq 'cp312|abi3'; then
  echo "warning: no cp312/abi3 wheel found; the workload image ($WORKLOAD_IMAGE) runs Python 3.12" >&2
fi
echo "  found: $(basename "${wheels[-1]}")"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "▶ KIND cluster $CLUSTER_NAME already exists, reusing it"
else
  echo "▶ Creating KIND cluster $CLUSTER_NAME"
  rendered_config="$(mktemp)"
  trap 'rm -f "$rendered_config"' EXIT
  sed "s|__ROAR_DIST_DIR__|$DIST_DIR|g" "$HARNESS_DIR/kind-config.yaml" >"$rendered_config"
  kind create cluster --name "$CLUSTER_NAME" --config "$rendered_config" --wait 180s
fi

echo "▶ Ensuring namespace $NAMESPACE"
kubectl_ctx create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl_ctx apply -f -

echo "▶ Wiring in-cluster glaas Service to the host glaas-api"
gateway_ip="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [[ -z "$gateway_ip" ]]; then
  echo "error: could not resolve the kind docker network gateway IP" >&2
  exit 1
fi
host_glaas_port="${HOST_GLAAS_URL##*:}"
kubectl_ctx apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: glaas
  namespace: ${NAMESPACE}
spec:
  ports:
    - port: ${CLUSTER_GLAAS_PORT}
      targetPort: ${host_glaas_port}
---
apiVersion: v1
kind: Endpoints
metadata:
  name: glaas
  namespace: ${NAMESPACE}
subsets:
  - addresses:
      - ip: ${gateway_ip}
    ports:
      - port: ${host_glaas_port}
EOF

echo "▶ Pre-pulling workload image on all nodes"
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
  docker exec "$node" crictl pull "$WORKLOAD_IMAGE" >/dev/null &
done
wait

if ((SKIP_GLAAS == 0)); then
  echo "▶ Checking host glaas-api at $HOST_GLAAS_URL"
  if ! curl -fsS --max-time 5 "$HOST_GLAAS_URL/api/v1/health" >/dev/null; then
    echo "error: glaas-api is not reachable at $HOST_GLAAS_URL" >&2
    echo "hint: start it (e.g. pm2 start glaas-api) or pass --skip-glaas" >&2
    exit 1
  fi

  echo "▶ Preflight: pod -> glaas reachability probe"
  kubectl_ctx -n "$NAMESPACE" delete pod roar-glaas-probe --ignore-not-found >/dev/null
  kubectl_ctx -n "$NAMESPACE" run roar-glaas-probe \
    --image="$WORKLOAD_IMAGE" --restart=Never --command -- \
    python -c "import urllib.request; r = urllib.request.urlopen('http://glaas:${CLUSTER_GLAAS_PORT}/api/v1/health', timeout=10); print('glaas reachable from pod:', r.status)"
  if ! kubectl_ctx -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded \
    pod/roar-glaas-probe --timeout=120s >/dev/null; then
    echo "error: probe pod could not reach glaas from inside the cluster" >&2
    kubectl_ctx -n "$NAMESPACE" describe pod roar-glaas-probe >&2 || true
    kubectl_ctx -n "$NAMESPACE" logs roar-glaas-probe >&2 || true
    echo "hint: is glaas-api listening on 0.0.0.0? is a firewall blocking ${gateway_ip}?" >&2
    exit 1
  fi
  kubectl_ctx -n "$NAMESPACE" logs roar-glaas-probe
  kubectl_ctx -n "$NAMESPACE" delete pod roar-glaas-probe >/dev/null
fi

if ((WITH_MINIO == 1)); then
  echo "▶ Deploying MinIO"
  kubectl_ctx apply -f "$HARNESS_DIR/manifests/minio.yaml"
  kubectl_ctx -n "$NAMESPACE" rollout status deployment/minio --timeout=180s
fi

if ((WITH_JOBSET == 1)); then
  echo "▶ Installing JobSet controller ${JOBSET_VERSION}"
  kubectl_ctx apply --server-side \
    -f "https://github.com/kubernetes-sigs/jobset/releases/download/${JOBSET_VERSION}/manifests.yaml"
  kubectl_ctx -n jobset-system rollout status deployment/jobset-controller-manager --timeout=300s
fi

echo
echo "✓ Harness ready"
echo "  cluster:            $CLUSTER_NAME (context $KUBE_CONTEXT)"
echo "  namespace:          $NAMESPACE"
echo "  host glaas URL:     $HOST_GLAAS_URL"
echo "  cluster glaas URL:  http://glaas:${CLUSTER_GLAAS_PORT} (via ${gateway_ip})"
if ((WITH_MINIO == 1)); then
  echo "  MinIO (host):       http://localhost:39000"
  echo "  MinIO (cluster):    http://minio:9000"
fi
echo
echo "Run the smoke tests:"
echo "  pytest tests/backends/k8s/e2e -o addopts='' -m k8s_e2e -v"
