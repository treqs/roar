#!/usr/bin/env bash
set -euo pipefail

# Deploys the roar lineage mutating webhook into the KIND harness via the
# Helm chart (deploy/charts/roar-lineage-webhook) — the chart is the
# artifact under test; the harness only supplies self-signed TLS material
# (no cert-manager dependency here).
#
# Env overrides: WEBHOOK_GLAAS_URL (server-visible from the webhook pod),
# WEBHOOK_CLUSTER_GLAAS_URL (visible from injected workload pods).

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
TOOLS_BIN="$HARNESS_DIR/.tools/bin"
export PATH="$TOOLS_BIN:$PATH"

HELM_VERSION="v3.16.4"
CLUSTER_NAME="roar-k8s-e2e"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
WEBHOOK_NS="roar-system"
RELEASE="roar"
# Must match the chart's fullname template: <release>-roar-lineage-webhook
SERVICE="${RELEASE}-roar-lineage-webhook"
CHART_DIR="$REPO_ROOT/deploy/charts/roar-lineage-webhook"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-roar-runtime:dev}"
WEBHOOK_GLAAS_URL="${WEBHOOK_GLAAS_URL:-http://glaas.roar-e2e.svc.cluster.local:3001}"
WEBHOOK_CLUSTER_GLAAS_URL="${WEBHOOK_CLUSTER_GLAAS_URL:-http://glaas.roar-e2e.svc.cluster.local:3001}"

if ! command -v helm >/dev/null 2>&1; then
  echo "▶ Downloading helm ${HELM_VERSION}"
  tmp="$(mktemp -d)"
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C "$tmp"
  install -m 0755 "$tmp/linux-amd64/helm" "$TOOLS_BIN/helm"
  rm -rf "$tmp"
fi

kubectl_ctx() { kubectl --context "$KUBE_CONTEXT" "$@"; }

echo "▶ Generating webhook TLS material"
CERT_DIR="$(mktemp -d)"
trap 'rm -rf "$CERT_DIR"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$CERT_DIR/ca.key" -out "$CERT_DIR/ca.crt" \
  -subj "/CN=roar-webhook-ca" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.csr" \
  -subj "/CN=${SERVICE}.${WEBHOOK_NS}.svc" >/dev/null 2>&1
openssl x509 -req -in "$CERT_DIR/tls.csr" -days 30 \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/tls.crt" \
  -extfile <(printf "subjectAltName=DNS:%s.%s.svc,DNS:%s.%s.svc.cluster.local" \
    "$SERVICE" "$WEBHOOK_NS" "$SERVICE" "$WEBHOOK_NS") >/dev/null 2>&1
CA_BUNDLE="$(base64 -w0 <"$CERT_DIR/ca.crt")"

echo "▶ Installing chart ${CHART_DIR}"
kubectl_ctx create namespace "$WEBHOOK_NS" --dry-run=client -o yaml | kubectl_ctx apply -f -
kubectl_ctx -n "$WEBHOOK_NS" create secret tls "${SERVICE}-tls" \
  --cert="$CERT_DIR/tls.crt" --key="$CERT_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl_ctx apply -f -

helm upgrade --install "$RELEASE" "$CHART_DIR" \
  --kube-context "$KUBE_CONTEXT" \
  --namespace "$WEBHOOK_NS" \
  --set image.repository="${RUNTIME_IMAGE%%:*}" \
  --set image.tag="${RUNTIME_IMAGE##*:}" \
  --set glaas.url="$WEBHOOK_GLAAS_URL" \
  --set glaas.clusterUrl="$WEBHOOK_CLUSTER_GLAAS_URL" \
  --set tls.secretName="${SERVICE}-tls" \
  --set tls.caBundle="$CA_BUNDLE" \
  --wait --timeout 180s

echo "✓ Webhook chart deployed (opt-in: label namespaces with roar.glaas.ai/lineage=enabled)"
