from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from roar.backends.k8s.manifest import rewrite_manifest_for_lineage
from roar.backends.k8s.pod_entry import _load_proxy_log_refs

from .conftest import SINGLE_JOB_MANIFEST


def _rewrite_proxy_mode(documents: list[dict[str, Any]], **overrides: Any):
    kwargs: dict[str, Any] = {
        "secret_name": "roar-fragment-abc",
        "session_id": "session-1",
        "fragment_token": "ff" * 32,
        "requirement": "roar-cli==0.3.7",
        "cluster_glaas_url": "http://glaas:3001",
        "tracer": "preload",
        "parent_job_uid": "cafe0123",
        "runtime_source": "image",
        "runtime_image": "roar-runtime:dev",
        "proxy_sidecar": True,
        "proxy_upstream": "http://minio:9000",
    }
    kwargs.update(overrides)
    return rewrite_manifest_for_lineage(documents, **kwargs)


def _job_pod_spec(rewrite) -> dict[str, Any]:
    job = next(doc for doc in rewrite.documents if doc.get("kind") == "Job")
    return job["spec"]["template"]["spec"]


def test_proxy_sidecar_injected_as_native_sidecar() -> None:
    rewrite = _rewrite_proxy_mode([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    pod_spec = _job_pod_spec(rewrite)

    sidecar = next(c for c in pod_spec["initContainers"] if c["name"] == "roar-s3-proxy")
    assert sidecar["restartPolicy"] == "Always"
    assert sidecar["image"] == "roar-runtime:dev"
    assert "roar-proxy --port 19191" in sidecar["command"][2]
    assert "--upstream http://minio:9000" in sidecar["command"][2]
    probe_command = sidecar["startupProbe"]["exec"]["command"]
    assert "socket.create_connection(('127.0.0.1', 19191)" in probe_command[2]

    volumes = {v["name"] for v in pod_spec["volumes"]}
    assert "roar-proxy-log" in volumes

    container = pod_spec["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["AWS_ENDPOINT_URL"]["value"] == "http://127.0.0.1:19191"
    assert env["ROAR_K8S_PROXY_LOG"]["value"] == "/roar-proxy-log/proxy.log"
    mounts = {m["name"] for m in container["volumeMounts"]}
    assert "roar-proxy-log" in mounts


def test_sidecar_inherits_workload_aws_credentials() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["spec"]["template"]["spec"]["containers"][0]["env"].extend(
        [
            {"name": "AWS_ACCESS_KEY_ID", "value": "AKIA..."},
            {
                "name": "AWS_SECRET_ACCESS_KEY",
                "valueFrom": {"secretKeyRef": {"name": "aws", "key": "secret"}},
            },
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
            {"name": "UNRELATED", "value": "no"},
        ]
    )
    rewrite = _rewrite_proxy_mode([manifest])
    sidecar = next(
        c for c in _job_pod_spec(rewrite)["initContainers"] if c["name"] == "roar-s3-proxy"
    )
    names = [e["name"] for e in sidecar["env"]]
    assert names == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"]
    assert sidecar["env"][1]["valueFrom"]["secretKeyRef"]["name"] == "aws"


def test_user_endpoint_url_wins_over_proxy_redirect() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "AWS_ENDPOINT_URL", "value": "http://user-endpoint:9000"}
    )
    rewrite = _rewrite_proxy_mode([manifest])
    container = _job_pod_spec(rewrite)["containers"][0]
    endpoints = [e for e in container["env"] if e["name"] == "AWS_ENDPOINT_URL"]
    assert endpoints == [{"name": "AWS_ENDPOINT_URL", "value": "http://user-endpoint:9000"}]


def test_proxy_requires_image_staging() -> None:
    rewrite = _rewrite_proxy_mode(
        [copy.deepcopy(SINGLE_JOB_MANIFEST)],
        runtime_source="install",
        runtime_image="",
    )
    pod_spec = _job_pod_spec(rewrite)
    assert "initContainers" not in pod_spec
    env_names = {e["name"] for e in pod_spec["containers"][0]["env"]}
    assert "AWS_ENDPOINT_URL" not in env_names


def test_proxy_sidecar_is_idempotent() -> None:
    rewrite = _rewrite_proxy_mode([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    job = next(doc for doc in rewrite.documents if doc.get("kind") == "Job")
    second = _rewrite_proxy_mode([copy.deepcopy(job)])
    pod_spec = _job_pod_spec(second)
    sidecars = [c for c in pod_spec["initContainers"] if c["name"] == "roar-s3-proxy"]
    assert len(sidecars) == 1


def test_proxy_log_refs_parse_and_defer_to_hooks(tmp_path: Path) -> None:
    log = tmp_path / "proxy.log"
    log.write_text(
        "\n".join(
            [
                "[S3:GetObject] s3://data/train.csv  (100 bytes)  etag=aaa  session=s job=j",
                "[S3:PutObject] s3://models/out.bin  (64 bytes)  etag=bbb",
                "[S3:GetObject] s3://data/seen-by-hooks.csv  etag=ccc",
                "[S3:ListObjectsV2] s3://data/  ",
                "not a log line",
            ]
        ),
        encoding="utf-8",
    )

    reads, writes = _load_proxy_log_refs(
        str(log),
        seen_reads={"s3://data/seen-by-hooks.csv"},
        seen_writes=set(),
    )
    assert [r["path"] for r in reads] == ["s3://data/train.csv"]
    assert reads[0]["capture_method"] == "proxy"
    assert reads[0]["hash"] == "aaa"
    assert [w["path"] for w in writes] == ["s3://models/out.bin"]
    assert writes[0]["size"] == 64


def test_proxy_log_refs_missing_file() -> None:
    assert _load_proxy_log_refs(None, seen_reads=set(), seen_writes=set()) == ([], [])
    assert _load_proxy_log_refs("/nonexistent/proxy.log", seen_reads=set(), seen_writes=set()) == (
        [],
        [],
    )
