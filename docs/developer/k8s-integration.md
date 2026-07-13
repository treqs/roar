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
in the dev meta-repo): Phases 1–2 implemented (submit wrapping, operator
adapters, multi-pod capture, `roar k8s attach`, bundle-mode fallback,
object-store I/O hooks). Remaining: mount-map path rewriting, RayJob
delegation, kill-pod retry chaos coverage, and the admission-webhook
injector.

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

  The `roar-proxy` S3 sidecar is deliberately not part of the CLI-side
  backend: the hooks win on attribution and avoid `AWS_ENDPOINT_URL`
  rewiring (which explicit-`endpoint_url` clients bypass). The proxy joins
  in the Phase-3 webhook injector as an opt-in sidecar for non-Python S3
  clients (see the design doc's Phase 3).

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
`runtime_install_requirement`, `cluster_glaas_url`, `bundle_dir`,
`wait_for_completion`, `wait_timeout_seconds`, `poll_interval_seconds`,
`fragment_session_ttl_seconds`. Env overrides beat config:
`ROAR_CLUSTER_PIP_REQ`, `ROAR_CLUSTER_GLAAS_URL`.

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
