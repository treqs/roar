from __future__ import annotations

import base64
import importlib
import json
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roar.execution.fragments.sessions import (
    generate_fragment_session,
    load_fragment_session,
    save_fragment_session,
)


def _module():
    return importlib.import_module("roar.backends.ray.fragment_reconstituter")


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return self._body


def _encrypt_batch(token: str, fragments: list[dict], nonce_byte: int) -> str:
    key = bytes.fromhex(token)
    nonce = bytes([nonce_byte]) * 12
    plaintext = json.dumps(fragments, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def _fragments_payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps({"fragments": items}, separators=(",", ":")).encode("utf-8")


def _wrapped_fragments_payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"success": True, "data": {"fragments": items}, "meta": {"page": 1}},
        separators=(",", ":"),
    ).encode("utf-8")


def test_fetch_batches_reads_wrapped_glaas_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    response_body = _wrapped_fragments_payload(
        [
            {"sequence": 1, "encrypted_batch": "batch-1"},
            {"sequence": 0, "encrypted_batch": "batch-0"},
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request
        assert timeout == 5
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    reconstituter = module.FragmentReconstituter(
        session_id="session-fetch-wrapped",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )

    assert reconstituter._fetch_batches() == [
        {"sequence": 0, "encrypted_batch": "batch-0"},
        {"sequence": 1, "encrypted_batch": "batch-1"},
    ]


def test_fetch_batches_supports_flat_response_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    response_body = _fragments_payload(
        [
            {"sequence": 1, "encrypted_batch": "batch-1"},
            {"sequence": 0, "encrypted_batch": "batch-0"},
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request
        assert timeout == 5
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    reconstituter = module.FragmentReconstituter(
        session_id="session-fetch-flat",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )

    assert reconstituter._fetch_batches() == [
        {"sequence": 0, "encrypted_batch": "batch-0"},
        {"sequence": 1, "encrypted_batch": "batch-1"},
    ]


def test_fetch_batches_returns_empty_list_for_empty_wrapped_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    response_body = _wrapped_fragments_payload([])

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    reconstituter = module.FragmentReconstituter(
        session_id="session-fetch-empty",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )

    assert reconstituter._fetch_batches() == []


def test_fetch_batches_warns_and_returns_empty_when_fragments_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    response_body = json.dumps({"success": True}, separators=(",", ":")).encode("utf-8")

    warnings: list[str] = []

    class _FakeLogger:
        def warning(self, message: str, *args: object) -> None:
            warnings.append(message % args if args else message)

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "_get_logger", lambda: _FakeLogger())
    reconstituter = module.FragmentReconstituter(
        session_id="session-fetch-missing",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )

    assert reconstituter._fetch_batches() == []
    assert warnings
    assert "missing fragments list" in warnings[0]


def test_reconstitute_decrypts_wrapped_glaas_response_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "0f" * 32
    fragment = {
        "job_uid": "job-glaas",
        "ray_task_id": "task-glaas",
        "command": "echo wrapped",
    }
    response_body = _wrapped_fragments_payload(
        [{"sequence": 0, "encrypted_batch": _encrypt_batch(token, [fragment], 9)}]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    merged_fragments: list[list[dict]] = []

    def _fake_collect_fragments(*args, **kwargs) -> None:
        if args:
            merged_fragments.append(list(args[0]))
            return
        merged_fragments.append(list(kwargs["fragments"]))

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", _fake_collect_fragments)

    result = module.FragmentReconstituter(
        session_id="session-reconstitute-wrapped",
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert merged_fragments == [[fragment]]
    assert result.fragments_processed == 1


def test_reconstitute_fetches_decrypts_and_merges_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "ab" * 32
    session_id = "session-1"

    sequence_0 = [{"job_uid": "job-a"}]
    sequence_1 = [{"job_uid": "job-b"}]
    response_body = _fragments_payload(
        [
            {"sequence": 1, "encrypted_batch": _encrypt_batch(token, sequence_1, 1)},
            {"sequence": 0, "encrypted_batch": _encrypt_batch(token, sequence_0, 0)},
        ]
    )

    captured_requests: list[urllib.request.Request] = []

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        captured_requests.append(request)
        assert timeout == 5
        return _FakeHttpResponse(response_body)

    merged_fragments: list[list[dict]] = []

    def _fake_collect_fragments(*args, **kwargs) -> None:
        if args:
            merged_fragments.append(list(args[0]))
            return
        merged_fragments.append(list(kwargs["fragments"]))

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", _fake_collect_fragments)

    result = module.FragmentReconstituter(
        session_id=session_id,
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.full_url.endswith(f"/api/v1/fragments/sessions/{session_id}/fragments")
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-roar-fragment-token"] == token

    assert merged_fragments == [[{"job_uid": "job-a"}, {"job_uid": "job-b"}]]
    assert result.fragments_processed == 2


def test_resolve_s3_key_placeholders_rewrites_paths_when_concrete_match_exists() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "phase-training",
            "writes": [
                {
                    "path": "roar+s3key://S3_MODELS_BUCKET/models/run-1/model.json",
                    "capture_method": "python",
                }
            ],
            "reads": [
                {
                    "path": "roar+s3key://S3_DATA_BUCKET/sensor_data/shard_000001.parquet",
                    "capture_method": "python",
                }
            ],
        },
        {
            "job_uid": "worker-train",
            "writes": [
                {
                    "path": "s3://output-bucket/models/run-1/model.json",
                    "capture_method": "proxy",
                }
            ],
            "reads": [
                {
                    "path": "s3://test-bucket/sensor_data/shard_000001.parquet",
                    "capture_method": "proxy",
                }
            ],
        },
    ]

    resolved = module.FragmentReconstituter._resolve_s3_key_placeholders(fragments)

    assert resolved[0]["writes"][0]["path"] == "s3://output-bucket/models/run-1/model.json"
    assert resolved[0]["reads"][0]["path"] == "s3://test-bucket/sensor_data/shard_000001.parquet"


def test_resolve_s3_key_placeholders_keeps_ambiguous_key_unresolved() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "phase",
            "writes": [
                {
                    "path": "roar+s3key://S3_RESULTS_BUCKET/shared/output.json",
                    "capture_method": "python",
                }
            ],
        },
        {
            "job_uid": "worker-a",
            "writes": [{"path": "s3://bucket-a/shared/output.json", "capture_method": "proxy"}],
        },
        {
            "job_uid": "worker-b",
            "writes": [{"path": "s3://bucket-b/shared/output.json", "capture_method": "proxy"}],
        },
    ]

    resolved = module.FragmentReconstituter._resolve_s3_key_placeholders(fragments)

    assert resolved[0]["writes"][0]["path"] == "roar+s3key://S3_RESULTS_BUCKET/shared/output.json"


def test_drop_proxy_fallback_duplicates_removes_driver_proxy_refs_owned_elsewhere() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "phase-eval",
            "function_name": "evaluation",
            "reads": [{"path": "s3://bucket/model.json"}],
            "writes": [{"path": "s3://bucket/metrics.json"}],
        },
        {
            "job_uid": "driver-proxy",
            "function_name": "s3_driver_proxy",
            "reads": [{"path": "s3://bucket/model.json"}],
            "writes": [
                {"path": "s3://bucket/metrics.json"},
                {"path": "s3://bucket/other.json"},
            ],
        },
    ]

    filtered = module.FragmentReconstituter._drop_proxy_fallback_duplicates(fragments)

    assert filtered[1]["reads"] == []
    assert filtered[1]["writes"] == [{"path": "s3://bucket/other.json"}]


def test_deduplicate_fragments_keeps_same_path_refs_for_distinct_task_identities() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "deadbeef",
            "parent_job_uid": "driver-main",
            "ray_task_id": "task-1",
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "proxy"}],
        },
        {
            "job_uid": "deadbeef",
            "parent_job_uid": "driver-main",
            "ray_task_id": "task-2",
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "python"}],
        },
    ]

    deduplicated = module.FragmentReconstituter._deduplicate_fragments(fragments)

    assert deduplicated[0]["reads"] == [
        {"path": "s3://bucket/input.json", "capture_method": "proxy"}
    ]
    assert deduplicated[1]["reads"] == [
        {"path": "s3://bucket/input.json", "capture_method": "python"}
    ]


def test_deduplicate_fragments_keeps_same_path_refs_for_distinct_legacy_job_uids() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "deadbeef",
            "ray_task_id": "task-1",
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "proxy"}],
        },
        {
            "job_uid": "feedface",
            "ray_task_id": "task-1",
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "python"}],
        },
    ]

    deduplicated = module.FragmentReconstituter._deduplicate_fragments(fragments)

    assert deduplicated[0]["reads"] == [
        {"path": "s3://bucket/input.json", "capture_method": "proxy"}
    ]
    assert deduplicated[1]["reads"] == [
        {"path": "s3://bucket/input.json", "capture_method": "python"}
    ]


def test_deduplicate_fragments_prefers_proxy_for_identity_less_duplicate_fragments() -> None:
    module = _module()
    fragments = [
        {
            "job_uid": "",
            "ray_task_id": "",
            "ray_worker_id": "worker-1",
            "ray_node_id": "node-1",
            "function_name": "process",
            "started_at": 1.0,
            "ended_at": 2.0,
            "exit_code": 0,
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "proxy"}],
        },
        {
            "job_uid": "",
            "ray_task_id": "",
            "ray_worker_id": "worker-1",
            "ray_node_id": "node-1",
            "function_name": "process",
            "started_at": 1.0,
            "ended_at": 2.0,
            "exit_code": 0,
            "reads": [{"path": "s3://bucket/input.json", "capture_method": "python"}],
        },
    ]

    deduplicated = module.FragmentReconstituter._deduplicate_fragments(fragments)

    assert deduplicated[0]["reads"] == [
        {"path": "s3://bucket/input.json", "capture_method": "proxy"}
    ]
    assert deduplicated[1]["reads"] == []


def test_reconstitute_is_idempotent_for_same_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "cd" * 32
    session_id = "session-2"
    fragments = [{"job_uid": "job-a"}, {"job_uid": "job-b"}]
    response_body = _fragments_payload(
        [{"sequence": 0, "encrypted_batch": _encrypt_batch(token, fragments, 2)}]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    merged_fragments: list[list[dict]] = []

    def _fake_collect_fragments(*args, **kwargs) -> None:
        if args:
            merged_fragments.append(list(args[0]))
            return
        merged_fragments.append(list(kwargs["fragments"]))

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", _fake_collect_fragments)

    reconstituter = module.FragmentReconstituter(
        session_id=session_id,
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    )
    first = reconstituter.reconstitute()
    second = reconstituter.reconstitute()

    assert merged_fragments == [fragments, fragments]
    assert first.fragments_processed == 2
    assert second.fragments_processed == 2


def test_reconstitute_noop_when_no_remote_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    token = "ef" * 32

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(_fragments_payload([]))

    merge_calls: list[object] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: merge_calls.append(1))

    result = module.FragmentReconstituter(
        session_id="session-3",
        token=token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert merge_calls == []
    assert result.fragments_processed == 0
    assert result.jobs_merged == 0
    assert result.artifacts_merged == 0


def test_reconstitute_handles_bad_token_without_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    good_token = "01" * 32
    wrong_token = "02" * 32
    response_body = _fragments_payload(
        [
            {
                "sequence": 0,
                "encrypted_batch": _encrypt_batch(good_token, [{"job_uid": "job-a"}], 3),
            }
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    merge_calls: list[object] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: merge_calls.append(1))

    result = module.FragmentReconstituter(
        session_id="session-4",
        token=wrong_token,
        glaas_url="http://localhost:3001",
        roar_db_path=tmp_path / ".roar" / "roar.db",
    ).reconstitute()

    assert merge_calls == []
    assert result.fragments_processed == 0


def test_fragment_key_file_is_retained_after_reconstitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    roar_dir = tmp_path / ".roar"
    key = generate_fragment_session()
    key_path = save_fragment_session(roar_dir, key)
    loaded_key = load_fragment_session(roar_dir, key["session_id"])
    response_body = _fragments_payload(
        [
            {
                "sequence": 0,
                "encrypted_batch": _encrypt_batch(
                    str(loaded_key["token"]), [{"job_uid": "job-a"}], 4
                ),
            }
        ]
    )

    def _fake_urlopen(request: urllib.request.Request, timeout: int = 0):
        del request, timeout
        return _FakeHttpResponse(response_body)

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(module, "collect_fragments", lambda *args, **kwargs: None)

    module.FragmentReconstituter(
        session_id=key["session_id"],
        token=str(loaded_key["token"]),
        glaas_url="http://localhost:3001",
        roar_db_path=roar_dir / "roar.db",
    ).reconstitute()

    assert key_path.exists()

