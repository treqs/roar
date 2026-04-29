"""Focused tests for Roar registration-package export."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from roar.application.publish.registration_package import (
    ExportRegistrationPackageRequest,
    build_registration_package,
    compute_registration_package_sha256,
    export_registration_package,
)
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext


def _lineage() -> LineageData:
    return LineageData(
        jobs=[
            {
                "id": 1,
                "job_uid": "job-1",
                "parent_job_uid": None,
                "timestamp": 123.0,
                "command": "python train.py --api-key sk-abcdefghijklmnopqrstuvwxyz",
                "script": "train.py",
                "session_id": 7,
                "step_number": 1,
                "git_commit": "deadbeef",
                "git_branch": "main",
                "duration_seconds": 4.5,
                "exit_code": 0,
                "job_type": "run",
                "metadata": '{"token":"ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"}',
                "_inputs": [
                    {
                        "hash": "a" * 64,
                        "path": "data/input.txt",
                        "byte_ranges": [[0, 10]],
                    }
                ],
                "_outputs": [{"hash": "b" * 64, "path": "models/model.bin"}],
                "_input_hashes": ["a" * 64],
                "_output_hashes": ["b" * 64],
            }
        ],
        artifacts=[
            {
                "id": "artifact-input",
                "size": 10,
                "first_seen_at": 122.0,
                "first_seen_path": "data/input.txt",
                "kind": "primitive",
                "hashes": [{"algorithm": "blake3", "digest": "a" * 64}],
            },
            {
                "id": "artifact-output",
                "size": 20,
                "first_seen_at": 124.0,
                "first_seen_path": "models/model.bin",
                "kind": "primitive",
                "hashes": [{"algorithm": "blake3", "digest": "b" * 64}],
            },
        ],
        artifact_hashes={"a" * 64, "b" * 64},
        pipeline={"id": 7},
    )


def test_registration_package_shape_digest_counts_and_redaction(tmp_path: Path) -> None:
    package, encoded = build_registration_package(
        session={"id": 7, "hash": "local-hash", "created_at": 123.0},
        lineage=_lineage(),
        git_context=GitContext(
            repo="https://user:secret-token@example.com/repo.git",
            commit="deadbeef",
            branch="main",
        ),
        cwd=tmp_path,
        created_at="2026-04-29T00:00:00Z",
        producer_version="test-version",
    )

    assert package["format"] == "roar.registration_package"
    assert package["version"] == 1
    assert package["producer"] == {"name": "roar", "version": "test-version"}
    assert package["source"] == {
        "local_session_id": "7",
        "git_repo": "https://user:[REDACTED]@example.com/repo.git",
        "git_commit": "deadbeef",
        "git_branch": "main",
    }

    manifest = package["manifest"]
    assert manifest["job_count"] == 1
    assert manifest["artifact_count"] == 2
    assert manifest["link_count"] == 2
    assert manifest["uncompressed_size_bytes"] == len(encoded)
    assert manifest["package_sha256"] == compute_registration_package_sha256(package)

    parsed = json.loads(encoded)
    assert parsed == package
    assert parsed["jobs"][0]["command"] == "python train.py --api-key=[REDACTED]"
    assert "[GITHUB_TOKEN_REDACTED]" in parsed["jobs"][0]["metadata"]
    assert parsed["links"] == [
        {
            "artifact_hash": "a" * 64,
            "byte_ranges": [[0, 10]],
            "direction": "input",
            "job_uid": "job-1",
            "path": "data/input.txt",
        },
        {
            "artifact_hash": "b" * 64,
            "direction": "output",
            "job_uid": "job-1",
            "path": "models/model.bin",
        },
    ]
    assert package["redaction"]["applied"] is True
    assert {warning["severity"] for warning in package["redaction"]["warnings"]} == {"warning"}


def test_export_registration_package_writes_package_without_glaas_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import roar.application.publish.registration_package as module
    from roar.integrations.glaas.client import GlaasClient

    output = tmp_path / "lineage.roarreg"
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    db_ctx.sessions.get_active.return_value = {
        "id": 7,
        "hash": "local-hash",
        "created_at": 123.0,
    }
    collector = MagicMock()
    collector.collect_session.return_value = _lineage()

    def fail_glaas_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("GLaaS client must not be constructed")

    monkeypatch.setattr(module, "create_database_context", lambda _roar_dir: db_ctx)
    monkeypatch.setattr(module, "LineageCollector", lambda: collector)
    monkeypatch.setattr(
        module,
        "resolve_roar_git_context",
        lambda _cwd, logger: GitContext(repo="repo", commit="deadbeef", branch="main"),
    )
    monkeypatch.setattr(GlaasClient, "__init__", fail_glaas_client)

    response = export_registration_package(
        ExportRegistrationPackageRequest(
            output=output,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
        )
    )

    assert response.success is True
    assert output.exists()
    package = json.loads(output.read_text(encoding="utf-8"))
    assert response.package_sha256 == package["manifest"]["package_sha256"]
    assert response.job_count == 1
    assert response.artifact_count == 2
    assert response.link_count == 2
    collector.collect_session.assert_called_once_with(7, tmp_path / ".roar")
