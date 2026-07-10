# k8s Lineage E2E Harness (Tier 1)

KIND-based harness for pressure-testing roar lineage capture in Kubernetes
training pods (`design-docs/k8s-training-lineage-integration.md`).

Three test layers share this harness:

- `e2e/test_k8s_product_path.py` — the Phase-1 product path through the real
  `roar.backends.k8s` backend: `roar run kubectl apply -f job.yaml` with a
  roar-unaware manifest, plan-time rewriting, Secret-delivered credentials,
  wheel served to pods over HTTP, and shared-finalizer reconstitution into
  the submitting project's `.roar/roar.db`. This is the confidence test.
- `e2e/test_k8s_distributed.py` — Phase-2 coverage: two-pod Indexed Job with
  completion-index identity, child-process capture, and a cross-pod artifact
  edge over a shared volume; `roar k8s attach` from a fresh project via the
  cluster Secret; and a JobSet run through the real controller (skipped
  unless bootstrapped with `--with-jobset`).
- `e2e/test_k8s_fallback_s3.py` — bundle-mode fallback (black-hole cluster
  GLaaS URL, bundle written to a shared volume, pulled off the node and
  merged with `roar k8s ingest-bundles`) and in-pod S3 capture (MinIO via
  `--with-minio`: boto3 get/put recorded as `s3://` lineage refs with etag
  hashes).
- `e2e/test_k8s_smoke.py` — the Phase-0 runtime diagnostic: fixtures
  hand-wrap the manifest (no backend involved) to isolate the runtime pieces
  (in-pod tracing, fragment streaming, identity contract) when the product
  path breaks.

Unit tests for the backend (manifest rewriting, command matching, planning)
live in `unit/` and run in the default gate — no cluster needed.

## Prerequisites

- Docker
- A packaged wheel: `bash scripts/build_wheel_with_bins.sh` (repo root)
- Local glaas-api on `http://localhost:3001` (e.g. via pm2)
- `kind`/`kubectl` are downloaded automatically into `.tools/bin` if missing

## Usage

```bash
# one-time (and after wheel changes)
bash scripts/build_wheel_with_bins.sh

# create cluster + wire glaas + preflight
# (--with-minio for S3 scenarios, --with-jobset for the JobSet operator e2e)
bash tests/backends/k8s/scripts/bootstrap_k8s.sh --with-jobset --with-minio

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
