#!/usr/bin/env bash
set -euo pipefail

# Deploys the roar lineage mutating webhook into the KIND harness.
#
# - self-signed CA + server cert (no cert-manager dependency in the harness)
# - roar-runtime:dev image serves the webhook (built/loaded by caller)
# - namespaceSelector opt-in: roar.glaas.ai/lineage=enabled
# - failurePolicy Ignore: webhook outages never block workloads
#
# Env overrides: WEBHOOK_GLAAS_URL (server-visible from the webhook pod),
# WEBHOOK_CLUSTER_GLAAS_URL (visible from injected workload pods).

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_BIN="$HARNESS_DIR/.tools/bin"
export PATH="$TOOLS_BIN:$PATH"

CLUSTER_NAME="roar-k8s-e2e"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
WEBHOOK_NS="roar-system"
SERVICE="roar-webhook"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-roar-runtime:dev}"
WEBHOOK_GLAAS_URL="${WEBHOOK_GLAAS_URL:-http://glaas.roar-e2e.svc.cluster.local:3001}"
WEBHOOK_CLUSTER_GLAAS_URL="${WEBHOOK_CLUSTER_GLAAS_URL:-http://glaas.roar-e2e.svc.cluster.local:3001}"

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

echo "▶ Deploying webhook to namespace $WEBHOOK_NS"
kubectl_ctx create namespace "$WEBHOOK_NS" --dry-run=client -o yaml | kubectl_ctx apply -f -
kubectl_ctx -n "$WEBHOOK_NS" create secret tls roar-webhook-tls \
  --cert="$CERT_DIR/tls.crt" --key="$CERT_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl_ctx apply -f -

kubectl_ctx apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: roar-webhook
  namespace: ${WEBHOOK_NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: roar-webhook-secrets
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: roar-webhook-secrets
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: roar-webhook-secrets
subjects:
  - kind: ServiceAccount
    name: roar-webhook
    namespace: ${WEBHOOK_NS}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: roar-webhook
  namespace: ${WEBHOOK_NS}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: roar-webhook
  template:
    metadata:
      labels:
        app: roar-webhook
    spec:
      serviceAccountName: roar-webhook
      containers:
        - name: webhook
          image: ${RUNTIME_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - python
            - -m
            - roar.backends.k8s.webhook
            - --port=8443
            - --cert=/tls/tls.crt
            - --key=/tls/tls.key
          env:
            - name: ROAR_WEBHOOK_GLAAS_URL
              value: ${WEBHOOK_GLAAS_URL}
            - name: ROAR_WEBHOOK_CLUSTER_GLAAS_URL
              value: ${WEBHOOK_CLUSTER_GLAAS_URL}
            - name: ROAR_WEBHOOK_RUNTIME_SOURCE
              value: image
            - name: ROAR_WEBHOOK_RUNTIME_IMAGE
              value: ${RUNTIME_IMAGE}
            - name: ROAR_NO_TELEMETRY
              value: "1"
          ports:
            - containerPort: 8443
          volumeMounts:
            - name: tls
              mountPath: /tls
              readOnly: true
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8443
              scheme: HTTPS
            initialDelaySeconds: 2
            periodSeconds: 3
      volumes:
        - name: tls
          secret:
            secretName: roar-webhook-tls
---
apiVersion: v1
kind: Service
metadata:
  name: ${SERVICE}
  namespace: ${WEBHOOK_NS}
spec:
  selector:
    app: roar-webhook
  ports:
    - port: 443
      targetPort: 8443
---
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: roar-lineage-injector
webhooks:
  - name: lineage.roar.glaas.ai
    admissionReviewVersions: ["v1"]
    sideEffects: NoneOnDryRun
    failurePolicy: Ignore
    reinvocationPolicy: IfNeeded
    timeoutSeconds: 10
    namespaceSelector:
      matchLabels:
        roar.glaas.ai/lineage: enabled
    clientConfig:
      service:
        name: ${SERVICE}
        namespace: ${WEBHOOK_NS}
        path: /mutate
      caBundle: ${CA_BUNDLE}
    rules:
      - apiGroups: ["batch"]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["jobs"]
      - apiGroups: ["jobset.x-k8s.io"]
        apiVersions: ["*"]
        operations: ["CREATE"]
        resources: ["jobsets"]
      - apiGroups: ["kubeflow.org"]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["pytorchjobs"]
      - apiGroups: ["trainer.kubeflow.org"]
        apiVersions: ["*"]
        operations: ["CREATE"]
        resources: ["trainjobs"]
      - apiGroups: ["ray.io"]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["rayjobs"]
EOF

kubectl_ctx -n "$WEBHOOK_NS" rollout status deployment/roar-webhook --timeout=180s
echo "✓ Webhook deployed (opt-in: label namespaces with roar.glaas.ai/lineage=enabled)"
