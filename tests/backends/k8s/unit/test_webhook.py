from __future__ import annotations

import base64
import copy
import json
from typing import Any

from roar.backends.k8s.webhook import (
    ANNOTATION_PARENT_UID,
    WebhookSettings,
    mutate_admission_review,
)

from .conftest import SINGLE_JOB_MANIFEST

SETTINGS = WebhookSettings(
    glaas_url="http://glaas.roar-e2e.svc.cluster.local:3001",
    cluster_glaas_url="http://glaas.roar-e2e.svc.cluster.local:3001",
    runtime_source="image",
    runtime_image="roar-runtime:dev",
)


class _Spy:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("secret boom")
        self.calls.append(("secret", namespace, name, sorted(data)))

    def register_session(self, session_id: str, token_hash: str) -> None:
        if self.fail:
            raise RuntimeError("glaas boom")
        self.calls.append(("session", session_id))


def _review(obj: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "req-1",
            "namespace": "ml-auto",
            "dryRun": dry_run,
            "object": obj,
        },
    }


def _decode_patch(result: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(base64.b64decode(result["response"]["patch"]))


def test_job_admission_is_instrumented() -> None:
    spy = _Spy()
    result = mutate_admission_review(
        _review(copy.deepcopy(SINGLE_JOB_MANIFEST)),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )

    response = result["response"]
    assert response["allowed"] is True
    assert response["patchType"] == "JSONPatch"

    kinds = [call[0] for call in spy.calls]
    assert kinds == ["session", "secret"]
    secret_call = next(call for call in spy.calls if call[0] == "secret")
    assert secret_call[1] == "ml-auto"  # request namespace, not manifest's
    assert secret_call[3] == ["session_id", "token"]

    patch = _decode_patch(result)
    spec_patch = next(op for op in patch if op["path"] == "/spec")
    command = spec_patch["value"]["template"]["spec"]["containers"][0]["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert "roar.backends.k8s.pod_entry" in command[2]
    init_names = [
        c["name"] for c in spec_patch["value"]["template"]["spec"].get("initContainers", [])
    ]
    assert "roar-runtime-staging" in init_names

    annotations_patch = next(op for op in patch if op["path"] == "/metadata/annotations")
    assert ANNOTATION_PARENT_UID in annotations_patch["value"]


def test_non_workload_objects_pass_through() -> None:
    spy = _Spy()
    result = mutate_admission_review(
        _review({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}}),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )
    assert result["response"]["allowed"] is True
    assert "patch" not in result["response"]
    assert spy.calls == []


def test_opt_out_label_skips_injection() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["metadata"].setdefault("labels", {})["roar.glaas.ai/lineage"] = "disabled"
    spy = _Spy()
    result = mutate_admission_review(
        _review(manifest),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )
    assert "patch" not in result["response"]
    assert spy.calls == []


def test_already_instrumented_is_idempotent() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["metadata"].setdefault("annotations", {})[ANNOTATION_PARENT_UID] = "cafe0123"
    spy = _Spy()
    result = mutate_admission_review(
        _review(manifest),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )
    assert "patch" not in result["response"]
    assert spy.calls == []


def test_dry_run_has_no_side_effects() -> None:
    spy = _Spy()
    result = mutate_admission_review(
        _review(copy.deepcopy(SINGLE_JOB_MANIFEST), dry_run=True),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )
    assert result["response"]["allowed"] is True
    assert "patch" not in result["response"]
    assert spy.calls == []


def test_failures_never_block_admission() -> None:
    spy = _Spy(fail=True)
    result = mutate_admission_review(
        _review(copy.deepcopy(SINGLE_JOB_MANIFEST)),
        settings=SETTINGS,
        create_secret=spy.create_secret,
        register_session=spy.register_session,
    )
    response = result["response"]
    assert response["allowed"] is True
    assert "patch" not in response
    assert any("lineage injection skipped" in w for w in response.get("warnings", []))


def test_settings_from_environ() -> None:
    settings = WebhookSettings.from_environ(
        {
            "ROAR_WEBHOOK_GLAAS_URL": "http://glaas:3001",
            "ROAR_WEBHOOK_CLUSTER_GLAAS_URL": "http://glaas.ns.svc:3001",
            "ROAR_WEBHOOK_RUNTIME_SOURCE": "image",
            "ROAR_WEBHOOK_RUNTIME_IMAGE": "roar-runtime:v1",
            "ROAR_WEBHOOK_MOUNT_MAP": '{"/data": "gs://bucket"}',
        }
    )
    assert settings.glaas_url == "http://glaas:3001"
    assert settings.runtime_image == "roar-runtime:v1"
    assert settings.mount_map == {"/data": "gs://bucket"}
