# Single-pod training Job template for the Tier-1 smoke test.
#
# Rendered by tests/backends/k8s/e2e/conftest.py with string.Template.
# This is the hand-wrapped stand-in for what `roar k8s prepare` will
# eventually generate: runtime staged from a hostPath wheel mount, the
# command wrapped through roar, fragment-session credentials from a
# Secret (never inline env), and identity from the downward API.
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 1800
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: roar-k8s-e2e
    spec:
      restartPolicy: Never
      volumes:
        - name: roar-wheels
          hostPath:
            path: /roar-dist
            type: Directory
        - name: workload
          configMap:
            name: ${configmap_name}
        - name: work
          emptyDir: {}
      containers:
        - name: trainer
          image: python:3.12-slim
          command: ["bash", "/workload/roar-wrapper.sh"]
          workingDir: /work
          env:
            - name: ROAR_SESSION_ID
              valueFrom:
                secretKeyRef:
                  name: ${secret_name}
                  key: session_id
            - name: ROAR_FRAGMENT_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${secret_name}
                  key: token
            - name: GLAAS_URL
              value: http://glaas:3001
            - name: ROAR_NO_TELEMETRY
              value: "1"
            - name: ROAR_K8S_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            - name: ROAR_K8S_POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: ROAR_K8S_POD_UID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.uid
            - name: ROAR_K8S_NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          volumeMounts:
            - name: roar-wheels
              mountPath: /wheels
              readOnly: true
            - name: workload
              mountPath: /workload
              readOnly: true
            - name: work
              mountPath: /work
