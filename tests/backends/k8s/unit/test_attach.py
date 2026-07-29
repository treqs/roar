from __future__ import annotations

import copy
from typing import Any

from roar.backends.k8s.attach import (
    _instrumented_env_entries,
    _session_secret_name,
)
from roar.backends.k8s.manifest import (
    rewrite_manifest_for_lineage,
    workload_kind_for_document,
)
from roar.backends.k8s.workload_wait import terminal_condition

from .conftest import SINGLE_JOB_MANIFEST
from .test_workload_adapters import JOBSET_MANIFEST, TRAINJOB_MANIFEST


def _rewritten_workload(manifest: dict[str, Any]) -> dict[str, Any]:
    rewrite = rewrite_manifest_for_lineage(
        [copy.deepcopy(manifest)],
        secret_name="roar-fragment-deadbeef",
        session_id="session-1",
        fragment_token="ff" * 32,
        requirement="roar-cli",
        cluster_glaas_url="http://glaas:3001",
        tracer="preload",
        parent_job_uid="cafe0123",
    )
    return next(doc for doc in rewrite.documents if doc.get("kind") != "Secret")


def test_attach_recovers_identity_from_rewritten_job() -> None:
    doc = _rewritten_workload(SINGLE_JOB_MANIFEST)
    workload_kind = workload_kind_for_document(doc)
    assert workload_kind is not None

    env = _instrumented_env_entries(doc, workload_kind)
    values = {entry["name"]: entry for entry in env}
    assert values["ROAR_K8S_PARENT_JOB_UID"]["value"] == "cafe0123"
    assert _session_secret_name(env) == "roar-fragment-deadbeef"


def test_attach_recovers_identity_from_jobset_and_trainjob() -> None:
    for manifest in (JOBSET_MANIFEST, TRAINJOB_MANIFEST):
        doc = _rewritten_workload(manifest)
        workload_kind = workload_kind_for_document(doc)
        assert workload_kind is not None
        env = _instrumented_env_entries(doc, workload_kind)
        assert _session_secret_name(env) == "roar-fragment-deadbeef"


def test_kind_aliases_cover_every_supported_workload() -> None:
    """Every WORKLOAD_KINDS entry must be reachable via KIND/NAME attach."""
    from roar.backends.k8s.attach import _KIND_ALIASES
    from roar.backends.k8s.manifest import WORKLOAD_KINDS

    aliased_resources = set(_KIND_ALIASES.values())
    for kind in WORKLOAD_KINDS:
        assert kind.kubectl_resource in aliased_resources, (
            f"{kind.kind} ({kind.kubectl_resource}) has no attach alias"
        )
        assert kind.kind.lower() in _KIND_ALIASES


def test_attach_reports_uninstrumented_workload() -> None:
    doc = copy.deepcopy(SINGLE_JOB_MANIFEST)
    workload_kind = workload_kind_for_document(doc)
    assert workload_kind is not None
    env = _instrumented_env_entries(doc, workload_kind)
    assert env == []
    assert _session_secret_name(env) == ""


def test_terminal_condition_union_across_kinds() -> None:
    def doc_with(condition_type: str) -> dict[str, Any]:
        return {"status": {"conditions": [{"type": condition_type, "status": "True"}]}}

    for success in ("Complete", "Completed", "Succeeded", "SuccessCriteriaMet"):
        succeeded, _message = terminal_condition(doc_with(success))
        assert succeeded is True, success
    for failure in ("Failed", "FailureTarget"):
        succeeded, _message = terminal_condition(doc_with(failure))
        assert succeeded is False, failure

    succeeded, _message = terminal_condition({"status": {"conditions": []}})
    assert succeeded is None
