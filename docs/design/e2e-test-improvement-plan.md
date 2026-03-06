# E2E Test Improvement Plan (Ray + GLaaS)

## Problem Statement

This plan addresses the dead-test gap described in the task: three production regressions were not caught because Ray e2e tests are effectively disabled.

Root causes in current repo state:
- `pyproject.toml` globally ignores `tests/e2e` via `--ignore=tests/e2e`.
- CI (`.github/workflows/ci.yml`) does not start the Ray e2e Docker Compose stack.
- `tests/e2e/ray/docker-compose.yml` has Ray + MinIO only; no GLaaS API or GLaaS Postgres.
- Fragment/session/reconstitution tests target `http://localhost:3001`, so they skip when GLaaS is absent.

Consequence: 41 Ray e2e tests are collected when enabled, but they are not part of required CI signal today.

## Goals

1. Make Ray e2e tests reliably runnable **locally** against glaas-api on `:3001`.
2. Ensure fragment streaming + reconstitution path is tested against real GLaaS API behavior.
3. Add one "golden path" e2e that asserts every intermediate step in the broken production flow.
4. Keep local default `pytest` fast and non-flaky (e2e skipped unless explicitly requested).

## Constraint: No CI for E2E Tests

**glaas-api is a private codebase.** Docker images are not public. E2E tests must always
run locally against a glaas-api server on `localhost:3001`. There is no GitHub Actions
workflow for e2e tests. The `--ignore=tests/e2e` in `pyproject.toml` is correct for CI.

## Non-Goals

- No production feature changes in this iteration.
- No expansion of `tests/live_glaas/` scope beyond what is needed to support Ray e2e reliability.

## 1) GLaaS Prerequisite (External Dependency)

glaas-api is a **private codebase** — it cannot be added to docker-compose.yml or CI.
It must be running locally on `localhost:3001` before e2e tests execute.

### Proposed changes

- **Do NOT add glaas-api to docker-compose.yml.** Keep compose for Ray + MinIO only.
- Document the prerequisite in `tests/e2e/ray/README.md`:
  - glaas-api must be running on `:3001` with Postgres on `:5434`
  - Fragment endpoints (`/api/v1/fragments/sessions`) must be deployed
- Add a `make test-e2e` target to the Makefile that:
  1. Checks Ray cluster health (`http://localhost:8265/api/version`)
  2. Checks GLaaS health (`http://localhost:3001/api/v1/health`)
  3. Runs `pytest tests/e2e/ray/ -v --timeout=300`
- Tests already skip gracefully when services are unreachable (via `_skip_if_services_unreachable`)

### Estimated effort
- 0.5 day.

## 2) Local Developer Workflow (No CI)

E2E tests are **local-only** — glaas-api is private, no CI integration possible.

### Workflow

```bash
# Prerequisites (manual):
# 1. glaas-api running on :3001 with Postgres on :5434
# 2. Ray e2e Docker cluster:
docker compose -f tests/e2e/ray/docker-compose.yml up -d
# 3. Run tests:
make test-e2e
```

### Makefile target

```makefile
test-e2e:
	@echo "Checking prerequisites..."
	@curl -sf http://localhost:8265/api/version > /dev/null || (echo "Ray not running on :8265" && exit 1)
	@curl -sf http://localhost:3001/api/v1/health > /dev/null || (echo "GLaaS not running on :3001" && exit 1)
	pytest tests/e2e/ray/ -v --timeout=300
```

### Estimated effort
- 0.5 day.

## 3) Test Coverage Gap Review (`tests/e2e/ray/`, 41 tests)

### Inventory and disposition

#### Expected to pass once GLaaS is added (11 tests)

- `test_fragment_session_registration.py`
  - `test_roar_ray_submit_creates_fragment_key_file`
  - `test_session_is_preregistered_in_glaas_fragment_store`
  - `test_session_env_vars_visible_inside_ray_job`
- `test_fragment_streaming.py`
  - `test_file_io_job_streams_encrypted_fragments_to_glaas`
  - `test_fragment_list_is_non_empty_for_completed_session`
  - `test_fragments_are_opaque_ciphertext`
- `test_fragment_reconstitution.py`
  - `test_auto_reconstitution_populates_local_roar_db`
  - `test_reconstituted_artifact_hash_rows_are_present_and_correct`
  - `test_reconstitution_is_idempotent`
  - `test_fragment_key_file_is_retained`
- `test_infra_health.py`
  - `test_glaas_health_endpoint_responds`

#### Already independent of GLaaS; should remain runnable (10 tests)

- `test_harness_smoke.py`
  - `test_cluster_is_reachable`
  - `test_cluster_has_multiple_nodes`
  - `test_tasks_run_on_workers`
  - `test_minio_is_accessible`
  - `test_worker_local_filesystem_accessible`
- `test_infra_health.py`
  - `test_ray_head_dashboard_is_reachable`
  - `test_ray_job_submission_works`
  - `test_minio_is_reachable`
- `test_runtime_env_conflict.py`
  - `test_runtime_env_conflict_without_override`
  - `test_runtime_env_conflict_succeeds_with_override`

#### Need Phase 2 alignment updates (20 tests)

These were written around pre-fragments-only/local-DB assumptions (`submit_job_on_head(..., ROAR_WRAP=1)` + direct DB inspection on head container), and should be migrated to fragment-session + reconstitution-aware assertions.

- `test_file_io_capture.py` (3)
  - `TestFileIOCapture::test_worker_file_write_appears_as_output_artifact`
  - `TestFileIOCapture::test_worker_file_read_appears_as_input_artifact`
  - `TestFileIOCapture::test_pipeline_intermediate_files_captured`
- `test_multi_node_capture.py` (3)
  - `TestMultiNodeCapture::test_io_captured_from_worker_containers`
  - `TestMultiNodeCapture::test_worker_logs_merged_into_single_lineage_record`
  - `TestMultiNodeCapture::test_native_tracer_captures_non_python_io`
- `test_native_tracing.py` (1)
  - `TestNativeTracing::test_worker_ld_preload_and_artifact_capture`
- `test_ray_data_capture.py` (1)
  - `TestRayDataCapture::test_read_csv_and_write_parquet_are_captured`
- `test_s3_capture.py` (4)
  - `TestS3Capture::test_worker_s3_put_appears_as_output_artifact`
  - `TestS3Capture::test_worker_s3_get_appears_as_input_artifact`
  - `TestS3Capture::test_s3_artifact_has_etag`
  - `TestS3Capture::test_worker_s3_write_artifact_has_nonzero_size`
- `test_s3_pipeline.py` (5)
  - `TestS3Pipeline::test_all_s3_put_get_captured`
  - `TestS3Pipeline::test_cross_task_s3_artifact_identity`
  - `TestS3Pipeline::test_model_artifacts_have_cross_task_identity`
  - `TestS3Pipeline::test_no_orphaned_s3_artifacts`
  - `TestS3Pipeline::test_lineage_depth_reaches_raw_inputs`
- `test_task_attribution.py` (3)
  - `TestTaskAttribution::test_each_output_has_task_id`
  - `TestTaskAttribution::test_distinct_tasks_produce_distinct_attributions`
  - `TestTaskAttribution::test_reader_task_linked_to_writer_tasks`

### Update strategy for these 20 tests

- Convert test entrypoint to `roar run ray job submit ...` where applicable.
- Assert both remote fragment evidence (GLaaS endpoint) and local reconstituted DB evidence.
- Keep existing semantic intent (S3 capture, attribution, multi-node lineage), but anchor assertions on the fragments-only flow.

### Estimated effort
- 2.0-3.0 days for reliable migration and flake reduction.

## 4) Critical Golden Path E2E Test

Add one comprehensive test in `tests/e2e/ray/` (for example `test_fragment_golden_path.py`) that validates the exact production-broken flow.

### Flow under test

`roar run ray job submit` -> session registration -> runtime env token/session propagation -> worker file I/O -> `_emit_fragment` -> `GlaasFragmentStreamer` POST -> job completion -> reconstitution -> local DB populated.

### Required assertions (explicit checkpoints)

1. Submit command succeeds (exit code 0).
2. `.roar/fragment-sessions/*.key` exists with valid `session_id`, `token`, `token_hash`.
3. GLaaS session exists before/after execution (`GET /api/v1/fragments/sessions/{id}/fragments` returns 200).
4. Ray job output confirms runtime env contains `ROAR_SESSION_ID` and `ROAR_FRAGMENT_TOKEN` matching key file.
5. GLaaS returns at least one fragment batch for that session.
6. Each fragment batch includes non-empty `encrypted_batch`; plaintext marker is absent from ciphertext.
7. Batch `sequence` is monotonic (streamer behavior).
8. Submit output includes reconstitution marker (for example `[roar] lineage reconstituted:`).
9. Local `.roar/roar.db` has `jobs`, `artifacts`, `artifact_hashes`, and non-zero `job_inputs/job_outputs` links.
10. At least one local artifact path corresponds to the emitted worker I/O marker path.

### Failure diagnostics to include

- print submit stdout/stderr on failure
- dump GLaaS fragment response payload
- query DB counts and key rows on assertion failure

### Estimated effort
- 1.0-1.5 days including stabilization.

## 5) Remove `--ignore=tests/e2e` and fix local behavior

### Proposed pytest config changes

In `pyproject.toml`:
- remove `--ignore=tests/e2e` from `addopts`
- keep explicit markers (`ray_e2e`, `live_glaas`, etc.)
- make local default avoid running heavy e2e by marker expression (for example exclude `ray_e2e` by default)

In Ray e2e fixture (`tests/e2e/ray/conftest.py`):
- keep/strengthen graceful skip when Docker/Compose is unavailable (`pytest.skip`, not hard fail)
- ensure skip reason is explicit (missing Docker daemon/permissions/service health)

Net effect:
- default local `pytest` remains fast and green
- explicit `pytest tests/e2e/ray/ ...` runs when environment is prepared
- CI e2e workflow is authoritative for this lane

### Estimated effort
- 0.5 day.

## 6) Contract Testing for GLaaS Fragment Response

### Problem to prevent

`_fetch_batches` bug happened because client expected one shape while API returned another (`payload["fragments"]` vs `payload["data"]["fragments"]`).

### Recommended approach (hybrid)

1. **Consumer contract fixture in Roar**
   - keep canonical example payloads for both accepted shapes (wrapped + flat fallback during transition)
   - unit test parser against canonical fixtures

2. **Schema/OpenAPI validation gate**
   - validate GLaaS fragment endpoint response against a pinned OpenAPI schema (or extracted JSON Schema)
   - fail CI if endpoint shape drifts unexpectedly

3. **Real-service integration check in Ray e2e lane**
   - one test that calls the real GLaaS endpoint and asserts expected response envelope + fragment list presence

This gives fast local unit feedback plus end-to-end contract drift detection.

### Estimated effort
- 1.0-1.5 days, depending on OpenAPI/schema availability and pinning strategy.

## Priority Order

1. **P0:** Section 1 (GLaaS prerequisite docs) + Section 2 (Makefile target + local workflow).
2. **P0:** Section 4 (golden-path regression test) to directly cover the production failure chain.
3. **P0:** Section 5 (marker strategy — keep `--ignore=tests/e2e` for CI, add `make test-e2e`).
4. **P1:** Section 3 migration of 20 legacy tests to fragments-only semantics.
5. **P1:** Section 6 contract testing hardening.

Note: Section 2 from original plan (CI integration) removed — glaas-api is private.

## Risks and Tradeoffs

- **Cross-repo dependency risk (glaas-api image/source):** can destabilize CI if image/tag management is weak.
  - Mitigation: pin image digest or pin checked-out commit SHA.
- **Runtime cost:** 41 Docker-based tests can materially increase CI time.
  - Mitigation: separate workflow, cache layers, upload diagnostics to reduce rerun cost.
- **Flakiness from distributed startup/health timing:** Ray/GLaaS/MinIO races can create intermittent failures.
  - Mitigation: strict health checks + bounded retries + deterministic teardown.
- **False confidence if legacy tests remain semantically stale:** tests may pass while missing fragments-only regressions.
  - Mitigation: migrate 20 Phase-2-misaligned tests and add golden-path test first.

## Effort Summary

| Section | Effort |
|---|---|
| 1. Compose GLaaS/Postgres | 0.5-1.5 days |
| 2. CI integration | 1.0-1.5 days |
| 3. Coverage-gap updates | 2.0-3.0 days |
| 4. Critical golden test | 1.0-1.5 days |
| 5. Remove ignore + markers | 0.5 day |
| 6. Contract testing | 1.0-1.5 days |
| **Total** | **6.0-9.5 days** |

