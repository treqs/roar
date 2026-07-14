# Kubernetes Integration (Developer)

## 1. High-level summary

The k8s backend instruments Kubernetes training workloads submitted with
`kubectl apply|create -f <manifest>` through `roar run`. Unlike Ray (runtime
hooks) it works at the manifest layer, like OSMO — but transports lineage via
GLaaS fragment streaming, like Ray, rather than bundle files.

Supported workload kinds (exactly one per manifest; other documents pass
through): `batch/v1` Job (plain or Indexed), `jobset.x-k8s.io` JobSet,
`kubeflow.org/v1` PyTorchJob, and `trainer.kubeflow.org` TrainJob. All pods
of a workload share one fragment session and one parent job uid; per-pod
identity comes from the downward API plus the completion-index/node-rank
env chain (`JOB_COMPLETION_INDEX` → `PET_NODE_RANK` → pod-level `RANK`).

Current phase status (see `design-docs/k8s-training-lineage-integration.md`
in the dev meta-repo): Phases 1–3 implemented and live-validated
(submit wrapping, operator adapters incl. real training-operator v1 and
trainer v2 controllers, multi-pod capture, `roar k8s attach`, bundle-mode
fallback, object-store I/O hooks, mount-map rewriting, retry-chaos
coverage, RayJob delegation, the roar-runtime image, the mutating
webhook injector, and the opt-in proxy sidecar). Remaining from Phase 3:
a packaged Helm chart (the harness deploys via
`tests/backends/k8s/scripts/deploy_webhook.sh`).

**Runtime staging modes**: `k8s.runtime_source = "install"` pip-installs
`k8s.runtime_install_requirement` at container start; `"image"` stages
hermetic per-ABI trees (cp310–cp313) from `k8s.runtime_image` via a
`roar-runtime-staging` init container + emptyDir — no network at pod
start. The image (`deploy/roar-runtime/Dockerfile`,
`scripts/build_runtime_image.sh`) ships a generated top-level
`sitecustomize.py` per tree because PYTHONPATH staging does not process
`.pth` files. TrainJob keeps the install path (no inline pod template);
RayJob keeps its runtime-env pip mechanism.

**Webhook injector** (`webhook.py`): a stdlib HTTPS AdmissionReview
server (served by the roar-runtime image) intercepting CREATE of all
five workload kinds in namespaces labeled `roar.glaas.ai/lineage=enabled`.
It reuses the same manifest rewriter as the CLI path, mints the fragment
session against GLaaS, creates the credentials Secret through the k8s
API (never embedded in the object), annotates the workload
(`roar.glaas.ai/parent-uid`, `session-id`, `fragment-secret`), and
returns a JSONPatch. Idempotent under `reinvocationPolicy: IfNeeded` via
the parent-uid annotation; dry-run admissions are side-effect-free
(`sideEffects: NoneOnDryRun`); every internal failure returns allowed
with a warning and pairs with `failurePolicy: Ignore` — lineage never
blocks admission. Reconstitution is client-driven via `roar k8s attach`.
`ROAR_WEBHOOK_PROXY_SIDECAR=true` (+ optional
`ROAR_WEBHOOK_PROXY_UPSTREAM`) makes the injector add the proxy sidecar
to every workload it instruments.

**RayJob delegation** (`rayjob.py`): KubeRay overwrites container commands
with `ray start --block`, so command-wrapping can't see user code. Instead
the RayJob rewrite reuses the Ray backend's runtime surface: the
entrypoint is wrapped through the Ray driver entrypoint, `runtimeEnvYAML`
gains the roar pip requirement + worker setup hook + Ray env contract
(`ROAR_EXECUTION_BACKEND=ray`, `ROAR_JOB_ID` = the k8s parent uid so Ray
fragments link to the recorded submit job; node agents/proxy stay off per
the proxy decision), and fragment-session credentials go into the
RayCluster pod templates as Secret refs — never inline in the CR. RayJob
signals completion via `status.jobStatus` (not conditions), and
reconstitution/attach delegate to the **Ray** backend's reconstituter,
since the streamed fragments are Ray `TaskFragment` payloads that merge
as `ray_task` jobs.

Per-task capture fidelity in KubeRay workers is verified on ray 2.46 and
2.54 (the strict e2e runs against the native harness's 2.54 pin by
default; override with `ROAR_E2E_RAY_IMAGE` to smoke other versions). An
earlier diagnosis blamed 2.46's executor internals; empirically both
versions route task execution through the patched
`FunctionActorManager.get_execution_info` — the real gap was pathlib:
`roar_worker._startup` now patches `io.open` alongside `builtins.open`
(`Path.read_bytes`/`write_bytes` resolve `io.open` by module attribute,
and without the preload tracer those opens were invisible; on
preload-equipped native workers the duplicate python-level captures are
absorbed by the existing `native > python` dedup). If a future Ray moves
the executor internals, `roar_worker` now warns loudly instead of
silently emitting task fragments without identity or file refs.

**Mounted storage** (`mount_map.py`): FUSE CSI mounts surface object I/O as
local file syscalls under a mount path. The rewriter derives a per-container
mount map (inline CSI volumes it can see — GCS FUSE, Mountpoint-for-S3 —
plus explicit `[k8s.mount_map]` config for PVC-backed drivers whose bucket
lives in the cluster-side PV) and injects it as `ROAR_K8S_MOUNT_MAP`;
`pod_entry` stamps it into fragment metadata, and reconstitution rewrites
ref paths (`/data/foo` → `gs://bucket/foo`, longest prefix wins). Capture
stays raw in the fragment; the mapping used is auditable in metadata. PVC
mounts get a `pvc://claim` identity tag with no rewrite — their cross-pod
edges connect through content hashes.

**Retry semantics**: Job retries produce attempt-distinct lineage — each
attempt's fragment is keyed by pod UID inside the task identity, so a
failed first attempt and its successful retry land as separate `k8s_task`
jobs with their own outputs (covered by the chaos e2e).

Two capture channels feed each pod's fragment:

- **File I/O**: the preload tracer under `roar run` (process tree included).
- **Object-store I/O** (`object_io.py`): direct S3 access is HTTP, invisible
  to the tracer. The k8s backend's `RuntimeImportAdapter` (dispatched by
  `roar_inject.pth`/sitecustomize inside every `ROAR_WRAP` child) patches
  `botocore.client.BaseClient._make_api_call` (and aiobotocore's async
  variant, covering s3fs/fsspec) to append S3 data-op events to
  `ROAR_K8S_OBJECT_IO_FILE`; `pod_entry` folds them into the fragment as
  `s3://` refs with etag hashes. Ranged ``GetObject`` calls record their
  ``Range`` header as ``byte_ranges`` (ranges accumulate per object across
  events and flow through `ArtifactRef.byte_ranges` into
  `job_inputs/job_outputs.byte_ranges`). Hooks no-op outside pods (env
  unset) and never raise into user code.

- **Proxy sidecar** (opt-in, `k8s.proxy_sidecar = true`): for S3 clients
  the botocore hooks can't see (Go/Rust/Java binaries, plain HTTP). The
  rewriter appends a `roar-s3-proxy` **native sidecar** (an init container
  with `restartPolicy: Always`, k8s ≥ 1.29/GA 1.33) running the
  `roar-proxy` binary from the staged runtime tree — it therefore
  **requires image staging** (`k8s.runtime_source = "image"`); with
  install mode it warns and skips. Wrapped containers get
  `AWS_ENDPOINT_URL=http://127.0.0.1:19191` (only when the user hasn't
  set their own — explicit user endpoints win, and such clients simply
  bypass the proxy). The proxy logs each request to a shared emptyDir
  (`ROAR_K8S_PROXY_LOG`); `pod_entry` parses it via
  `roar.execution.cluster.proxy.parse_log_line` and folds refs into the
  fragment with `capture_method="proxy"`, skipping objects the hooks
  already captured (hooks win on attribution). Two gotchas verified live:
  `roar-proxy` is a **re-signing** reverse proxy, so the sidecar needs
  AWS credentials — the rewriter copies the workload container's
  `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/session/role env entries
  onto the sidecar (without them every forward 502s); and it binds
  loopback only, so its startup probe must be an `exec` probe (kubelet
  `tcpSocket` probes dial the pod IP, which never succeeds).

Transport is streaming-first with a bundle fallback: when `k8s.bundle_dir`
names a mounted shared volume and GLaaS is unreachable from the pod (probe
or non-streamed emit), `pod_entry` writes `roar-fragments-<pod>.json` there
instead; `roar k8s ingest-bundles <dir>` merges a host-visible copy later.
Note the fragment streamer swallows per-batch POST failures (reports
"streamed" regardless), which is why the fallback needs its own probe —
surfacing streamer failure counts is an open follow-up.

## 2. Flow

1. **Match** (`roar/backends/k8s/submit.py`): binary `kubectl`, verb
   `apply|create`, `-f` pointing at a manifest containing exactly one
   supported workload; gated by `k8s.enabled` (default off).
2. **Plan** (same module): pre-registers a GLaaS fragment session (saves the
   `.key` under `.roar/fragment-sessions/`), rewrites the manifest
   (`manifest.py`), writes it 0600 under `.roar/k8s/prepared/` with a
   `.context.json` sidecar, and returns the rewritten command plus
   `session_id` — the framework attaches the shared submit finalizer.
   GLaaS/registration failures degrade to an uninstrumented submit with a
   warning; lineage never blocks training.
3. **Rewrite** (`manifest.py`): a `WORKLOAD_KINDS` adapter registry locates
   pod templates per kind (Job: `spec.template.spec`; JobSet:
   `spec.replicatedJobs[*].template.spec.template.spec`; PyTorchJob:
   `spec.pytorchReplicaSpecs.{Role}.template.spec`; TrainJob has no inline
   template — its `spec.trainer.command/env` override is wrapped instead).
   Each container with an explicit `command` gets a `/bin/sh -c` script that
   pip-installs the roar runtime (`k8s.runtime_install_requirement`, default
   pinned `roar-cli`) and execs `python3 -m roar.backends.k8s.pod_entry "$@"`;
   on any bootstrap failure it falls back to exec'ing the original command
   uninstrumented. Injects the env contract (`GLAAS_URL` = cluster-visible
   URL, Secret-backed `ROAR_SESSION_ID`/`ROAR_FRAGMENT_TOKEN`, downward-API
   identity fields) and appends the Secret document. Containers without an
   explicit command are skipped with a warning; a workload with none fails
   actionably (ENTRYPOINT resolution is a later phase).
4. **Host execution** (`host_execution.py` + `workload_wait.py`): runs
   kubectl, deletes the prepared manifest (it embeds the token Secret),
   polls the workload to a terminal condition (`k8s.wait_for_completion`,
   default on; condition types unioned across kinds), and records the
   submit as a local job — with the **original** command and the
   plan-generated `parent_job_uid`, so reproduce re-enters the backend and
   pod fragments link to the submit node.
5. **In pod** (`pod_entry.py`): `roar init -n` + `roar run --tracer preload`
   around the original command, then exports the recorded job as an
   `ExecutionFragment` (task identity
   `pod_uid:container:completion_index:restart_attempt`, parent from
   `ROAR_K8S_PARENT_JOB_UID`) and streams it via the shared fragment
   transport. Best-effort: the training exit code always wins.
6. **Reconstitution** (`fragment_reconstituter.py` + `lineage.py`): the
   shared finalizer fetches/decrypts session batches, dedupes by task
   identity, and merges `k8s_task` jobs through `merge_execution_fragments`
   with `K8S_FRAGMENT_LINEAGE_BACKEND`.

## 3. CLI

`roar k8s prepare -f job.yaml -o prepared.yaml` runs the same rewriter for
inspection: no session is registered and no Secret is embedded; the user
creates the named Secret out of band or uses the managed path.

`roar k8s attach WORKLOAD [-n ns] [--context ctx] [--wait/--no-wait]
[--session-file key]` (`attach.py`) recovers lineage from an
already-submitted workload — the CI/fire-and-forget flow. It reads the
parent job uid and Secret name from the cluster object's own env contract,
resolves credentials (local `.key` → cluster Secret → `--session-file`),
optionally waits for completion, records a local `attach` job under the
recovered parent uid, and reconstitutes the streamed fragments.

## 4. Config

Section `k8s` (registered `BackendConfigAdapter`): `enabled`, `tracer`,
`runtime_source` (`install`|`image`), `runtime_image`,
`runtime_install_requirement`, `cluster_glaas_url`, `bundle_dir`,
`proxy_sidecar`, `proxy_upstream` (upstream S3 endpoint the proxy
forwards to; empty = AWS), `wait_for_completion`, `wait_timeout_seconds`,
`poll_interval_seconds`, `fragment_session_ttl_seconds`, plus the
`[k8s.mount_map]` table (config-file-only; maps mount paths to
object-store URIs). Env overrides beat config: `ROAR_CLUSTER_PIP_REQ`,
`ROAR_CLUSTER_GLAAS_URL`.

## 5. Tests

- Unit (default gate): `tests/backends/k8s/unit/` — matching, planning,
  rewriting, identity contract.
- E2E (KIND harness, `k8s_e2e` marker): `tests/backends/k8s/e2e/` — see
  `tests/backends/k8s/README.md` for the harness and the product-path vs
  runtime-diagnostic split.

## 6. Notes and caveats

- Host-visible vs cluster-visible endpoints stay separate on purpose
  (`glaas.url` vs `k8s.cluster_glaas_url`).
- The prepared manifest embeds the fragment token in a Secret document; it is
  written 0600 and deleted immediately after kubectl reads it. The token also
  lives in `.roar/fragment-sessions/*.key`, matching the Ray path.
- `glaas.url` has a hosted default, so the "GLaaS unconfigured" branch is
  effectively unreachable via config; degradation is driven by registration
  failure instead.
- The distributed adapter's worker-bootstrap hooks are inert stubs: pods are
  instrumented by manifest rewriting, not `roar-worker` entrypoints.
