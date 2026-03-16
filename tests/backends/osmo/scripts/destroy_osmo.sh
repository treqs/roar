#!/usr/bin/env bash

set -euo pipefail

cluster_name="${OSMO_KIND_CLUSTER_NAME:-roar-osmo-e2e}"
localstack_forward_pid="/tmp/osmo-localstack-port-forward.pid"

if [[ -f "${localstack_forward_pid}" ]]; then
  kill "$(cat "${localstack_forward_pid}")" >/dev/null 2>&1 || true
  rm -f "${localstack_forward_pid}"
fi

kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
