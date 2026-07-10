# Kubernetes Integration (Developer)

## 1. High-level summary

The k8s backend instruments plain `batch/v1` Jobs submitted with
`kubectl apply|create -f <manifest>` through `roar run`. Unlike Ray (runtime
hooks) it works at the manifest layer, like OSMO — but transports lineage via
GLaaS fragment streaming, like Ray, rather than bundle files.

Phase 1 scope (see `design-docs/k8s-training-lineage-integration.md` in the
dev meta-repo): exactly one Job per manifest, explicit container commands
only, streaming transport only. Kubeflow TrainJob/JobSet/RayJob adapters,
bundle fallback, `roar k8s attach`, and the admission-webhook injector are
later phases.

## 2. Flow

1. **Match** (`roar/backends/k8s/submit.py`): binary `kubectl`, verb
   `apply|create`, `-f` pointing at a manifest containing exactly one
   `batch/v1` Job; gated by `k8s.enabled` (default off).
2. **Plan** (same module): pre-registers a GLaaS fragment session (saves the
   `.key` under `.roar/fragment-sessions/`), rewrites the manifest
   (`manifest.py`), writes it 0600 under `.roar/k8s/prepared/` with a
   `.context.json` sidecar, and returns the rewritten command plus
   `session_id` — the framework attaches the shared submit finalizer.
   GLaaS/registration failures degrade to an uninstrumented submit with a
   warning; lineage never blocks training.
3. **Rewrite** (`manifest.py`): wraps each container that has an explicit
   `command` with a `/bin/sh -c` script that pip-installs the roar runtime
   (`k8s.runtime_install_requirement`, default pinned `roar-cli`) and execs
   `python3 -m roar.backends.k8s.pod_entry "$@"`; on any bootstrap failure it
   falls back to exec'ing the original command uninstrumented. Injects the
   env contract (`GLAAS_URL` = cluster-visible URL, Secret-backed
   `ROAR_SESSION_ID`/`ROAR_FRAGMENT_TOKEN`, downward-API identity fields) and
   appends the Secret document. Containers without an explicit command are
   skipped with a warning; a Job with none fails actionably (ENTRYPOINT
   resolution is a later phase).
4. **Host execution** (`host_execution.py`): runs kubectl, deletes the
   prepared manifest (it embeds the token Secret), polls the Job to a
   terminal condition (`k8s.wait_for_completion`, default on), and records
   the submit as a local job — with the **original** command and the
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

## 4. Config

Section `k8s` (registered `BackendConfigAdapter`): `enabled`, `tracer`,
`runtime_install_requirement`, `cluster_glaas_url`, `wait_for_completion`,
`wait_timeout_seconds`, `poll_interval_seconds`,
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
