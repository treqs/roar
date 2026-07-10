# k8s Lineage E2E Harness (Tier 1)

KIND-based harness for pressure-testing roar lineage capture in Kubernetes
training pods. This is the Phase-0/Tier-1 slice from
`design-docs/k8s-training-lineage-integration.md`: no `roar.backends.k8s`
exists yet — the fixtures hand-wrap a Job manifest the way the future
`roar k8s prepare` will, so the runtime assumptions (in-pod tracing, wheel
staging, fragment streaming, identity contract) are proven first.

## Prerequisites

- Docker
- A packaged wheel: `bash scripts/build_wheel_with_bins.sh` (repo root)
- Local glaas-api on `http://localhost:3001` (e.g. via pm2)
- `kind`/`kubectl` are downloaded automatically into `.tools/bin` if missing

## Usage

```bash
# one-time (and after wheel changes)
bash scripts/build_wheel_with_bins.sh

# create cluster + wire glaas + preflight (add --with-minio for S3 scenarios)
bash tests/backends/k8s/scripts/bootstrap_k8s.sh

# run the smoke tests (addopts override needed: e2e dirs are ignored by default)
pytest tests/backends/k8s/e2e -o addopts='' -m k8s_e2e -v

# tear down
bash tests/backends/k8s/scripts/destroy_k8s.sh
```

## Topology

- KIND cluster `roar-k8s-e2e`: 1 control plane + 2 workers, k8s 1.33
- `dist/` mounted into nodes at `/roar-dist` → pods install the wheel from a
  hostPath volume (no network fetch, no stale artifact URLs)
- Host-visible vs cluster-visible endpoints are modeled separately on purpose:
  - glaas: `http://localhost:3001` (host) vs `http://glaas:3001` (pods, via a
    Service/Endpoints pair pointing at the kind docker-network gateway)
  - MinIO (optional): `http://localhost:39000` (host) vs `http://minio:9000`
    (pods)

## What the smoke test proves

1. The packaged wheel installs and traces (preload) inside a vanilla
   `python:3.12-slim` pod.
2. A roar-unaware training script's file I/O is captured with content hashes.
3. Fragments stream from the pod to glaas-api through the encrypted
   fragment-session pipeline using Secret-delivered credentials.
4. Fragments carry the k8s identity contract
   (`pod_uid:container:completion_index:restart_attempt` + pod/node metadata).
5. Decrypted fragments merge into a local `.roar/roar.db` via the shared
   fragment lineage engine.

Infra diagnosis lives in `scripts/bootstrap_k8s.sh` (preflight probe), not in
the tests: if the tests skip or fail, re-run bootstrap first to separate
infra failures from product failures.
