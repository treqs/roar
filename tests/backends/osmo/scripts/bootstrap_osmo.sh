#!/usr/bin/env bash

set -euo pipefail

cluster_name="${OSMO_KIND_CLUSTER_NAME:-roar-osmo-e2e}"
base_url="${OSMO_BASE_URL:-http://quick-start.osmo:38080}"
repo_root="${REPO_ROOT:-/workspace/roar}"
kind_config="${repo_root}/tests/backends/osmo/kind-osmo-cluster-config.yaml"
kai_scheduler_version="${OSMO_KAI_SCHEDULER_VERSION:-v0.12.10}"
quick_start_chart_version="${OSMO_QUICK_START_CHART_VERSION:-1.0.1}"
localstack_port="${OSMO_LOCALSTACK_PORT:-34566}"
localstack_forward_log="/tmp/osmo-localstack-port-forward.log"
localstack_forward_pid="/tmp/osmo-localstack-port-forward.pid"
localstack_cluster_url="http://localstack-s3.osmo:4566"
localstack_override_url="http://127.0.0.1:${localstack_port}"
dockerhub_username="${OSMO_DOCKERHUB_USERNAME:-}"
dockerhub_password="${OSMO_DOCKERHUB_PASSWORD:-}"
test_python_image="${OSMO_TEST_PYTHON_IMAGE:-public.ecr.aws/docker/library/python:3.11-slim}"
preload_images="${OSMO_PRELOAD_DOCKERHUB_IMAGES:-ghcr.io/nvidia/kai-scheduler/admission:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/binder:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/operator:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/podgrouper:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/podgroupcontroller:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/queuecontroller:${kai_scheduler_version} ghcr.io/nvidia/kai-scheduler/scheduler:${kai_scheduler_version} postgres:15.1 redis:7.0 gresau/localstack-persist:latest busybox:1.37.0 alpine:3.18 alpine/k8s:1.28.4 alpine/curl:8.14.1 amazon/aws-cli:2.15.33 ${test_python_image}}"
preload_pull_retries="${OSMO_PRELOAD_PULL_RETRIES:-3}"
kind_nodes_csv=""

cleanup_port_forward() {
  if [[ -f "${localstack_forward_pid}" ]]; then
    kill "$(cat "${localstack_forward_pid}")" >/dev/null 2>&1 || true
    rm -f "${localstack_forward_pid}"
  fi
}

dockerhub_login() {
  if [[ -z "${dockerhub_username}" || -z "${dockerhub_password}" ]]; then
    return 0
  fi

  printf '%s' "${dockerhub_password}" | docker login \
    --username "${dockerhub_username}" \
    --password-stdin >/dev/null
}

resolve_preload_pull_image() {
  local image="$1"
  case "${image}" in
    public.ecr.aws/docker/library/*)
      printf '%s\n' "${image}"
      ;;
    postgres:*|redis:*|busybox:*|alpine:*|python:*-slim)
      printf 'public.ecr.aws/docker/library/%s\n' "${image}"
      ;;
    gresau/localstack-persist:*|alpine/k8s:*|alpine/curl:*|amazon/aws-cli:*)
      printf 'mirror.gcr.io/%s\n' "${image}"
      ;;
    *)
      printf '%s\n' "${image}"
      ;;
  esac
}

docker_pull_with_retries() {
  local image="$1"
  local attempt=1
  while true; do
    if docker pull "${image}" >/dev/null; then
      return 0
    fi
    if (( attempt >= preload_pull_retries )); then
      echo "failed to pull ${image} after ${preload_pull_retries} attempts" >&2
      return 1
    fi
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}

preload_dockerhub_images_into_kind() {
  local image
  local pull_image
  local target_nodes_csv
  if [[ -z "${kind_nodes_csv}" ]]; then
    echo "kind node list is empty; cannot preload Docker Hub images" >&2
    exit 1
  fi
  for image in ${preload_images}; do
    target_nodes_csv="$(resolve_preload_nodes_csv "${image}")"
    pull_image="$(resolve_preload_pull_image "${image}")"
    docker_pull_with_retries "${pull_image}"
    if [[ "${pull_image}" != "${image}" ]]; then
      docker tag "${pull_image}" "${image}"
    fi
    kind load docker-image \
      --name "${cluster_name}" \
      --nodes "${target_nodes_csv}" \
      "${image}" >/dev/null
  done
}

resolve_preload_nodes_csv() {
  local image="$1"
  local selector=""
  case "${image}" in
    postgres:*|redis:*|gresau/localstack-persist:*)
      selector="node_group=data"
      ;;
    alpine/k8s:*)
      selector="node_group=service"
      ;;
    python:*-slim|public.ecr.aws/docker/library/python:*-slim)
      selector="node_group=compute"
      ;;
    *)
      ;;
  esac

  if [[ -z "${selector}" ]]; then
    printf '%s\n' "${kind_nodes_csv}"
    return 0
  fi

  local selected_nodes_csv
  selected_nodes_csv="$(kubectl get nodes -l "${selector}" -o jsonpath='{range .items[*]}{.metadata.name}{","}{end}')"
  selected_nodes_csv="${selected_nodes_csv%,}"
  if [[ -z "${selected_nodes_csv}" ]]; then
    printf '%s\n' "${kind_nodes_csv}"
    return 0
  fi
  printf '%s\n' "${selected_nodes_csv}"
}

wait_for_osmo_dev_login() {
  local deadline=$((SECONDS + 600))
  while (( SECONDS < deadline )); do
    if osmo login "${base_url}" --method=dev --username=testuser >/tmp/osmo-login.out 2>/tmp/osmo-login.err; then
      return 0
    fi
    sleep 5
  done

  echo "Timed out waiting for OSMO dev login" >&2
  cat /tmp/osmo-login.err >&2 || true
  return 1
}

patch_local_osmo_data_override_url() {
  cat <<EOF >/root/.config/osmo/config.yaml
auth:
  data:
    s3://osmo:
      access_key: test
      access_key_id: test
      override_url: ${localstack_override_url}
      region: us-east-1
EOF
}

patch_osmo_pod_templates_for_kind() {
  local response
  response="$(
    curl \
      --max-time 30 \
      --retry 3 \
      --retry-delay 5 \
      --fail-with-body \
      -w 'HTTP_STATUS:%{http_code}' \
      -X PUT \
      -H "Content-Type: application/json" \
      -H "x-osmo-user: testuser" \
      "${base_url}/api/configs/pod_template" \
      -d '{
        "configs": {
          "default_compute": {
            "spec": {
              "containers": [
                {
                  "name": "{{USER_CONTAINER_NAME}}",
                  "env": [
                    {
                      "name": "AWS_ENDPOINT_URL_S3",
                      "value": "http://localstack-s3.osmo:4566"
                    },
                    {
                      "name": "AWS_S3_FORCE_PATH_STYLE",
                      "value": "true"
                    },
                    {
                      "name": "AWS_DEFAULT_REGION",
                      "value": "us-east-1"
                    },
                    {
                      "name": "OSMO_LOGIN_DEV",
                      "value": "true"
                    },
                    {
                      "name": "OSMO_SKIP_DATA_AUTH",
                      "value": "1"
                    }
                  ]
                },
                {
                  "name": "osmo-ctrl",
                  "env": [
                    {
                      "name": "AWS_ENDPOINT_URL_S3",
                      "value": "http://localstack-s3.osmo:4566"
                    },
                    {
                      "name": "AWS_S3_FORCE_PATH_STYLE",
                      "value": "true"
                    },
                    {
                      "name": "AWS_DEFAULT_REGION",
                      "value": "us-east-1"
                    },
                    {
                      "name": "OSMO_LOGIN_DEV",
                      "value": "true"
                    },
                    {
                      "name": "OSMO_SKIP_DATA_AUTH",
                      "value": "1"
                    }
                  ]
                }
              ],
              "nodeSelector": {
                "node_group": "compute"
              }
            }
          },
          "default_user": {
            "spec": {
              "containers": [
                {
                  "name": "{{USER_CONTAINER_NAME}}",
                  "resources": {
                    "limits": {
                      "cpu": "{{USER_CPU}}",
                      "memory": "{{USER_MEMORY}}",
                      "ephemeral-storage": "{{USER_STORAGE}}"
                    },
                    "requests": {
                      "cpu": "{{USER_CPU}}",
                      "memory": "{{USER_MEMORY}}",
                      "ephemeral-storage": "{{USER_STORAGE}}"
                    }
                  }
                }
              ]
            }
          }
        },
        "description": "Adapt OSMO pod templates for KIND-based roar tests"
      }'
  )"

  local http_code body
  http_code="$(printf '%s' "${response}" | grep -o 'HTTP_STATUS:[0-9]*' | cut -d: -f2)"
  body="$(printf '%s' "${response}" | sed 's/HTTP_STATUS:[0-9]*$//')"
  if [[ -z "${http_code}" || "${http_code}" -ge 400 ]]; then
    echo "Failed to patch OSMO pod templates for KIND." >&2
    echo "HTTP status: ${http_code:-unknown}" >&2
    echo "Response body: ${body}" >&2
    return 1
  fi
}

ensure_backend_operator_token() {
  osmo user create backend-operator --roles osmo-backend --format-type json >/tmp/osmo-backend-user.out 2>/tmp/osmo-backend-user.err || true
  osmo user update testuser --add-roles osmo-backend --format-type json >/tmp/osmo-backend-role.out 2>/tmp/osmo-backend-role.err || true

  local token_name="backend-operator-token-$(date +%s)"
  local token_json
  token_json="$(
    osmo token set "${token_name}" \
      --user backend-operator \
      --roles osmo-backend \
      --format-type json
  )"
  local token
  token="$(
    printf '%s' "${token_json}" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])'
  )"

  kubectl patch secret backend-operator-token \
    -n osmo \
    --type merge \
    -p "{\"stringData\":{\"token\":\"${token}\"}}" >/dev/null
}

patch_backend_operator_deployments() {
  local patch_file="/tmp/osmo-remove-wait-token.json"
  cat <<'JSON' >"${patch_file}"
[
  {"op":"remove","path":"/spec/template/spec/initContainers/1"}
]
JSON

  kubectl patch deployment osmo-osmo-backend-listener \
    -n osmo \
    --type json \
    --patch-file "${patch_file}" >/dev/null || true
  kubectl patch deployment osmo-osmo-backend-worker \
    -n osmo \
    --type json \
    --patch-file "${patch_file}" >/dev/null || true

  kubectl rollout restart deployment/osmo-osmo-backend-listener -n osmo >/dev/null
  kubectl rollout restart deployment/osmo-osmo-backend-worker -n osmo >/dev/null
  kubectl rollout status deployment/osmo-osmo-backend-listener -n osmo --timeout=5m
  kubectl rollout status deployment/osmo-osmo-backend-worker -n osmo --timeout=5m
}

main() {
  kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
  docker ps -a --format '{{.Names}}' | grep "^${cluster_name}-" | xargs -r docker rm -f >/dev/null 2>&1 || true
  cleanup_port_forward

  helm repo add osmo https://helm.ngc.nvidia.com/nvidia/osmo >/dev/null 2>&1 || true
  helm repo update >/dev/null

  dockerhub_login
  kind create cluster --name "${cluster_name}" --config "${kind_config}"
  deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    kind_nodes_csv="$(kind get nodes --name "${cluster_name}" | paste -sd, -)"
    if [[ -n "${kind_nodes_csv}" ]]; then
      break
    fi
    sleep 1
  done

  kubectl wait --for=condition=Ready node --all --timeout=5m
  preload_dockerhub_images_into_kind

  for node in $(docker ps --format '{{.Names}}' | grep "^${cluster_name}-"); do
    docker exec "${node}" sysctl -w \
      fs.inotify.max_user_instances=8192 \
      fs.inotify.max_user_watches=524288 >/dev/null
  done

  kubectl delete pod -n kube-system -l k8s-app=kube-proxy --ignore-not-found >/dev/null
  kubectl rollout status daemonset/kube-proxy -n kube-system --timeout=5m

  helm upgrade --install kai-scheduler \
    oci://ghcr.io/nvidia/kai-scheduler/kai-scheduler \
    --version "${kai_scheduler_version}" \
    --create-namespace \
    -n kai-scheduler \
    --set global.nodeSelector.node_group=kai-scheduler \
    --set "scheduler.additionalArgs[0]=--default-staleness-grace-period=-1s" \
    --set "scheduler.additionalArgs[1]=--update-pod-eviction-condition=true" \
    --wait \
    --timeout 15m

  helm upgrade --install osmo osmo/quick-start \
    --version "${quick_start_chart_version}" \
    --namespace osmo \
    --create-namespace \
    --timeout 20m

  kubectl patch configmap quick-start \
    -n osmo \
    --type merge \
    -p '{"data":{"proxy-body-size":"32m"}}' >/dev/null
  kubectl rollout restart deployment/quick-start -n osmo >/dev/null
  kubectl rollout status deployment/quick-start -n osmo --timeout=5m

  kubectl rollout status deployment/localstack-s3 -n osmo --timeout=5m
  wait_for_osmo_dev_login
  ensure_backend_operator_token
  patch_backend_operator_deployments
  patch_osmo_pod_templates_for_kind

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
    if wait_for_osmo_dev_login; then
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
          if osmo credential set quick-start-localstack \
            --type DATA \
            --payload \
            endpoint=s3://osmo \
            override_url="${localstack_cluster_url}" \
            region=us-east-1 \
            access_key_id=test \
            access_key=test >/tmp/osmo-credential.out 2>/tmp/osmo-credential.err \
            && osmo profile set bucket osmo >/tmp/osmo-profile.out 2>/tmp/osmo-profile.err
          then
            patch_local_osmo_data_override_url
            exit 0
          fi
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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
