#!/bin/bash
~/.openclaw/workspace/scripts/ralph.sh \
  --workdir /home/trevor/dev/roar \
  --task 'Phase 4 of docs/design/ray-fragment-store-impl-plan.md.
Working ONLY in /home/trevor/dev/roar on branch tb/ray-submit.

READ FIRST (required):
- docs/design/ray-fragment-store-impl-plan.md (section "Phase 4")
- docs/design/ray-lineage-fragment-store.md (reconstitution section)
- roar/ray/fragment_key.py (load_key)
- roar/ray/glaas_fragment_streamer.py (encryption spec — reconstituter inverts this)
- roar/ray/collector.py (collect_fragments() — reuse merge logic here if possible)
- roar/db/schema.py (tables: sessions, jobs, artifacts, artifact_hashes, job_inputs, job_outputs)
- roar/cli/commands/run.py (execute_and_report — where post-submit hook goes)
- roar/cli/commands/_ray_job_submit.py (what metadata it returns after rewrite)

TASK: Implement auto-reconstitution into local .roar/roar.db after roar run ray job submit.

STEP 1 — Write FAILING unit tests in tests/unit/ray/test_fragment_reconstituter.py:
- test_reconstitute_fetches_decrypts_and_merges_fragments
  (mock GLaaS GET returning 2 encrypted batches, verify DB merge called)
- test_reconstitute_is_idempotent_for_same_session
  (run twice, verify merge called with same data, no error)
- test_reconstitute_noop_when_no_remote_fragments
  (empty fragment list = no DB writes)
- test_reconstitute_handles_bad_token_without_db_writes
  (wrong token = decryption error = no DB writes, no exception raised)
- test_fragment_key_file_is_retained_after_reconstitution
  (key file still present after success)

Run: uv run pytest tests/unit/ray/test_fragment_reconstituter.py -v
Confirm FAIL.

STEP 2 — Create roar/ray/fragment_reconstituter.py:
class FragmentReconstituter:
  __init__(self, session_id: str, token: str, glaas_url: str, roar_db_path: Path)

  reconstitute(self) -> ReconstitutionResult:
    1. GET {glaas_url}/api/v1/fragments/sessions/{session_id}/fragments
       header: x-roar-fragment-token: token
    2. For each batch in response (ordered by sequence):
       - base64 decode encrypted_batch
       - split nonce (first 12 bytes) from ciphertext
       - decrypt: AESGCM(key).decrypt(nonce, ciphertext, None) -> plaintext bytes
       - parse JSON list -> list of fragment dicts
    3. Merge all fragment dicts into local DB using collect_fragments() or equivalent
    4. Return ReconstitutionResult(jobs_merged, artifacts_merged, fragments_processed)

  On decryption failure: log warning, skip that batch (do not fail entire reconstitution)
  On network failure: log warning, return empty result (non-fatal)

ReconstitutionResult: dataclass with jobs_merged: int, artifacts_merged: int, fragments_processed: int

STEP 3 — Wire auto-reconstitution into roar/cli/commands/run.py:
After execute_and_report() returns for a ray job submit command:
- If maybe_rewrite returned a session_id AND GLAAS_URL is set:
  - Load key from .roar/fragment-sessions/<session_id>.key
  - Instantiate FragmentReconstituter
  - Call reconstitute()
  - Print summary: "[roar] lineage reconstituted: N jobs, M artifacts"
  - Failure is non-fatal (warn, continue)

To wire this: _ray_job_submit.py must return session_id from maybe_rewrite_ray_job_submit()
alongside the rewritten argv. Check current return type and update if needed.

STEP 4 — Write FAILING e2e tests in tests/e2e/ray/test_fragment_reconstitution.py:
- test_auto_reconstitution_populates_local_roar_db
- test_reconstituted_artifact_hash_rows_are_present_and_correct
- test_reconstitution_is_idempotent
- test_fragment_key_file_is_retained
All pytest.mark.e2e, skip gracefully if services down.

STEP 5 — Run unit tests:
uv run pytest tests/unit/ray/test_fragment_reconstituter.py -v
All PASS.

Run full unit suite: uv run pytest tests/unit/ -q
No regressions allowed.

Commit: "feat(roar): Phase 4 — auto-reconstitute local lineage DB from fragment store"

Done:
  git log --oneline -4
  openclaw system event --text "RALPH_DONE: Phase 4 committed — auto-reconstitution complete" --mode now'
