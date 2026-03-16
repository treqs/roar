#!/usr/bin/env bash

set -euo pipefail

cluster_name="${OSMO_KIND_CLUSTER_NAME:-roar-osmo-e2e}"
base_url="${OSMO_BASE_URL:-http://quick-start.osmo:38080}"
repo_root="${REPO_ROOT:-/workspace/roar}"
kind_config="${repo_root}/tests/e2e/osmo/kind-osmo-cluster-config.yaml"
localstack_port="${OSMO_LOCALSTACK_PORT:-34566}"
localstack_forward_log="/tmp/osmo-localstack-port-forward.log"
localstack_forward_pid="/tmp/osmo-localstack-port-forward.pid"
localstack_override_url="http://127.0.0.1:${localstack_port}"

cleanup_port_forward() {
  if [[ -f "${localstack_forward_pid}" ]]; then
    kill "$(cat "${localstack_forward_pid}")" >/dev/null 2>&1 || true
    rm -f "${localstack_forward_pid}"
  fi
}

kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
cleanup_port_forward

helm repo add osmo https://helm.ngc.nvidia.com/nvidia/osmo >/dev/null 2>&1 || true
helm repo update >/dev/null

kind create cluster --name "${cluster_name}" --config "${kind_config}"

kubectl wait --for=condition=Ready node --all --timeout=5m

for node in $(docker ps --format '{{.Names}}' | grep "^${cluster_name}-"); do
  docker exec "${node}" sysctl -w \
    fs.inotify.max_user_instances=8192 \
    fs.inotify.max_user_watches=524288 >/dev/null
done

kubectl delete pod -n kube-system -l k8s-app=kube-proxy --ignore-not-found >/dev/null
kubectl rollout status daemonset/kube-proxy -n kube-system --timeout=5m

helm upgrade --install kai-scheduler \
  oci://ghcr.io/nvidia/kai-scheduler/kai-scheduler \
  --version v0.12.10 \
  --create-namespace \
  -n kai-scheduler \
  --set global.nodeSelector.node_group=kai-scheduler \
  --set "scheduler.additionalArgs[0]=--default-staleness-grace-period=-1s" \
  --set "scheduler.additionalArgs[1]=--update-pod-eviction-condition=true" \
  --wait \
  --timeout 15m

helm upgrade --install osmo osmo/quick-start \
  --namespace osmo \
  --create-namespace \
  --wait \
  --timeout 20m

kubectl rollout status deployment/localstack-s3 -n osmo --timeout=5m

nohup kubectl port-forward \
  --address 127.0.0.1 \
  -n osmo \
  service/localstack-s3 \
  "${localstack_port}:4566" >"${localstack_forward_log}" 2>&1 &
echo $! >"${localstack_forward_pid}"

deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  if curl -fsS "${localstack_override_url}/_localstack/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -fsS "${localstack_override_url}/_localstack/health" >/dev/null 2>&1; then
  echo "Timed out waiting for LocalStack port-forward" >&2
  cat "${localstack_forward_log}" >&2 || true
  exit 1
fi

deadline=$((SECONDS + 900))
while (( SECONDS < deadline )); do
  if osmo login "${base_url}" --method=dev --username=testuser >/tmp/osmo-login.out 2>/tmp/osmo-login.err; then
    if osmo pool list --format-type json >/tmp/osmo-pools.json 2>/tmp/osmo-pools.err; then
      if python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/osmo-pools.json").read_text())
pools = {
    pool.get("name"): pool
    for node_set in payload.get("node_sets", [])
    for pool in node_set.get("pools", [])
}
pool = pools.get("default")
raise SystemExit(0 if pool and pool.get("status") == "ONLINE" else 1)
PY
      then
        osmo credential set quick-start-localstack \
          --type DATA \
          --payload \
          endpoint=s3://osmo \
          region=us-east-1 \
          access_key_id=test \
          access_key=test >/tmp/osmo-credential.out 2>/tmp/osmo-credential.err
        osmo profile set bucket osmo >/tmp/osmo-profile.out 2>/tmp/osmo-profile.err
        exit 0
      fi
    fi
  fi
  sleep 5
done

echo "Timed out waiting for OSMO to become ready" >&2
kubectl get pods -A >&2 || true
cat /tmp/osmo-login.err >&2 || true
cat /tmp/osmo-pools.err >&2 || true
cat "${localstack_forward_log}" >&2 || true
cat /tmp/osmo-credential.err >&2 || true
cat /tmp/osmo-profile.err >&2 || true
exit 1
