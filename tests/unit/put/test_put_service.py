"""Focused mechanics tests for PutService."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from roar.application.publish.composite_builder import CompositeArtifactBuilder
from roar.application.publish.put_execution import PutService
from roar.application.publish.put_preparation import PreparedPutExecution
from roar.application.publish.registration import build_lineage_membership_index_payload
from roar.application.publish.results import PutDryRunItem
from roar.application.publish.source_resolution import ResolvedSource
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import (
    BatchRegistrationResult,
    GitContext,
    JobLinkResult,
    JobRegistrationResult,
)
from roar.integrations.glaas import GlaasClient
from roar.integrations.storage import MemoryBackend


def _create_mock_glaas_client() -> MagicMock:
    client = MagicMock(spec=GlaasClient)
    client.register_composite_artifact.return_value = (
        {"hash": "composite-hash-1", "created": True},
        None,
    )
    return client


def _create_mock_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=0,
        jobs_failed=0,
        artifacts_registered=0,
        artifacts_failed=0,
        links_created=0,
        links_failed=0,
        errors=[],
    )
    coordinator.job_service.create_job.return_value = JobRegistrationResult(
        success=True,
        job_uid="job-uid-1",
        job_id="job-1",
        error=None,
    )
    coordinator.job_service.link_job_artifacts.return_value = JobLinkResult(
        success=True,
        job_uid="job-uid-1",
        inputs_linked=1,
        outputs_linked=0,
        error=None,
    )
    return coordinator


def _create_mock_db() -> Mock:
    db = Mock()
    db.artifacts = Mock()
    db.artifacts.register.return_value = ("artifact-uuid-1", True)
    db.jobs = Mock()
    db.jobs.create.return_value = (42, "job-uid-1")
    db.sessions = Mock()
    db.sessions.get_next_step_number.return_value = 3
    return db


def _prepared_put(
    tmp_path: Path,
    *,
    sources: list[Path],
    glaas_client: MagicMock | None = None,
    session_id: int = 1,
    session_hash: str = "session_hash_abc123",
    session_url: str = "https://glaas.ai/dag/session_hash_abc123",
    destination_type: str = "memory",
    composite_source_type: str | None = None,
) -> PreparedPutExecution:
    resolved: list[ResolvedSource] = []
    for source in sources:
        source = source.resolve()
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    resolved.append(
                        ResolvedSource(
                            path=item.resolve(),
                            exists=True,
                            relative_key=str(item.relative_to(source)),
                            source_root=source,
                        )
                    )
            continue
        resolved.append(
            ResolvedSource(
                path=source,
                exists=True,
                relative_key=source.name,
            )
        )

    return PreparedPutExecution(
        glaas_client=glaas_client or _create_mock_glaas_client(),
        session_id=session_id,
        session_hash=session_hash,
        session_url=session_url,
        git_context=GitContext(
            repo="https://github.com/test/repo",
            branch="main",
            commit="deadbeef",
        ),
        resolved_sources=resolved,
        destination_type=destination_type,
        composite_source_type=composite_source_type,
    )


class TestPutService:
    def test_put_prepared_single_file_creates_job(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="test-bucket", prefix="models"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(return_value=LineageData()),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[model_file]),
            sources=[str(model_file)],
            message="publish model",
        )

        assert result.success is True
        assert result.job_id == 42
        assert len(result.uploaded_files) == 1
        assert result.uploaded_files[0].local_path == str(model_file.resolve())
        call_kwargs = service._db.jobs.create.call_args.kwargs
        assert call_kwargs["session_id"] == 1
        assert call_kwargs["step_number"] == 3
        assert call_kwargs["execution_backend"] == "local"
        assert call_kwargs["execution_role"] == "host"
        assert call_kwargs["job_type"] == "put"
        service._db.jobs.add_input.assert_called_once()

    def test_put_prepared_refreshes_and_syncs_put_job_labels(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        db = _create_mock_db()
        client = _create_mock_glaas_client()
        service = PutService(
            db_context=db,
            backend=MemoryBackend(bucket="test-bucket", prefix="models"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(return_value=LineageData()),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        with (
            patch(
                "roar.application.publish.put_execution.refresh_job_system_labels"
            ) as refresh_labels,
            patch("roar.application.publish.put_execution.sync_publish_labels") as sync_labels,
        ):
            result = service.put_prepared(
                prepared=_prepared_put(tmp_path, sources=[model_file], glaas_client=client),
                sources=[str(model_file)],
                message="publish model",
            )

        assert result.success is True
        refresh_labels.assert_called_once_with(db, job_id=42)
        sync_labels.assert_called_once()
        sync_kwargs = sync_labels.call_args.kwargs
        assert sync_kwargs["glaas_client"] is client
        assert sync_kwargs["db_ctx"] is db
        assert sync_kwargs["session_id"] is None
        assert sync_kwargs["session_hash"] == "session_hash_abc123"
        assert sync_kwargs["jobs"] == [
            {"id": 42, "job_uid": "job-uid-1", "remote_job_uid": "job-uid-1"}
        ]
        assert sync_kwargs["artifacts"] == []

    def test_put_prepared_returns_registered_session_info(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="test-bucket", prefix="models"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[model_file]),
            sources=[str(model_file)],
            message="publish model",
        )

        assert result.success is True
        assert result.session_hash == "session_hash_abc123"
        assert result.session_url == "https://glaas.ai/dag/session_hash_abc123"

    def test_put_prepared_registers_uploaded_artifacts_when_lineage_artifacts_are_empty(
        self, tmp_path: Path
    ) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        coordinator = _create_mock_coordinator()
        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="test-bucket", prefix="models"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=coordinator,
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[model_file]),
            sources=[str(model_file)],
            message="publish model",
        )

        assert result.success is True
        artifacts = coordinator.register_lineage.call_args.kwargs["artifacts"]
        assert len(artifacts) == 1
        assert artifacts[0]["hashes"][0]["algorithm"] == "blake3"
        assert artifacts[0]["session_hash"] == "session_hash_abc123"

    def test_put_prepared_hashes_multiple_files_in_single_batch_call(self, tmp_path: Path) -> None:
        file1 = tmp_path / "model1.pt"
        file2 = tmp_path / "model2.pt"
        file1.write_bytes(b"one")
        file2.write_bytes(b"two")

        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="bucket", prefix="run"),
            destination="memory://bucket/run",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        with patch("roar.application.publish.put_execution.hash_files_blake3") as hash_batch:
            hash_batch.return_value = {
                str(file1.resolve()): "a" * 64,
                str(file2.resolve()): "b" * 64,
            }
            result = service.put_prepared(
                prepared=_prepared_put(tmp_path, sources=[file1, file2]),
                sources=[str(file1), str(file2)],
                message="publish models",
            )

        assert result.success is True
        hash_batch.assert_called_once()

    def test_put_prepared_directory_source_registers_composite_artifact(
        self, tmp_path: Path
    ) -> None:
        # A structurally-detected dataset (>=2 parquet shards) forms a composite;
        # an unstructured pile of files deliberately does not (use --dataset for that).
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        (dataset_dir / "shard-0.parquet").write_text("a", encoding="utf-8")
        (dataset_dir / "shard-1.parquet").write_text("b", encoding="utf-8")

        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="bucket", prefix="run"),
            destination="memory://bucket/run",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[dataset_dir]),
            sources=[str(dataset_dir)],
            message="publish dataset",
        )

        assert result.success is True
        assert result.composites_registered

    def test_put_prepared_links_upstream_lineage_composites_as_inputs(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        upstream_root = tmp_path / "upstream"
        upstream_root.mkdir()
        (upstream_root / "part-1.parquet").write_text("one", encoding="utf-8")
        model_file.write_bytes(b"model data")

        db = _create_mock_db()
        db.composites = MagicMock()
        db.composites.get_components.return_value = [
            {
                "relative_path": "part-1.parquet",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "d" * 64,
                "component_size": 3,
                "component_type": "application/octet-stream",
            }
        ]
        db.composites.get_membership_index.return_value = None
        coordinator = _create_mock_coordinator()
        service = PutService(
            db_context=db,
            backend=MemoryBackend(bucket="bucket", prefix="run"),
            destination="memory://bucket/run",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=coordinator,
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[
                {
                    "id": "local-comp-1",
                    "kind": "composite",
                    "first_seen_path": str(upstream_root),
                    "hashes": [{"algorithm": "composite-blake3", "digest": "c" * 64}],
                    "component_count": 1,
                    "size": 10,
                }
            ],
            artifact_hashes={"c" * 64},
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[model_file]),
            sources=[str(model_file)],
            message="publish model",
        )

        assert result.success is True
        db.jobs.add_input.assert_any_call(42, "artifact-uuid-1", str(model_file.resolve()))
        db.jobs.add_input.assert_any_call(42, "local-comp-1", str(upstream_root))

    def test_put_prepared_marks_unexpected_composite_registration_response_unsuccessful(
        self, tmp_path: Path
    ) -> None:
        # A structurally-detected dataset (>=2 parquet shards) forms a composite;
        # an unstructured pile of files deliberately does not (use --dataset for that).
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        (dataset_dir / "shard-0.parquet").write_text("a", encoding="utf-8")
        (dataset_dir / "shard-1.parquet").write_text("b", encoding="utf-8")

        glaas_client = _create_mock_glaas_client()
        glaas_client.register_composite_artifact.return_value = "bad-response"
        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="bucket", prefix="run"),
            destination="memory://bucket/run",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        result = service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[dataset_dir], glaas_client=glaas_client),
            sources=[str(dataset_dir)],
            message="publish dataset",
        )

        assert result.success is False
        assert result.error is not None

    def test_put_prepared_dry_run_does_not_upload(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MagicMock()
        service = PutService(
            db_context=_create_mock_db(),
            backend=backend,
            destination="memory://bucket/run",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )

        with patch("roar.application.publish.put_execution.Spinner") as spinner_cls:
            result = service.put_prepared(
                prepared=_prepared_put(tmp_path, sources=[model_file]),
                sources=[str(model_file)],
                message="publish model",
                dry_run=True,
            )

        assert result.success is True
        assert result.dry_run is True
        assert result.session_hash == "session_hash_abc123"
        assert result.would_upload == [PutDryRunItem(path=str(model_file.resolve()), exists=True)]
        backend.upload.assert_not_called()
        spinner_cls.assert_not_called()

    def test_put_prepared_shows_spinner_during_publish(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        service = PutService(
            db_context=_create_mock_db(),
            backend=MemoryBackend(bucket="test-bucket", prefix="models"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        with patch("roar.application.publish.put_execution.Spinner") as spinner_cls:
            result = service.put_prepared(
                prepared=_prepared_put(tmp_path, sources=[model_file]),
                sources=[str(model_file)],
                message="publish model",
            )

        assert result.success is True
        # Hash + upload progress spinners precede the two publish-phase spinners.
        assert spinner_cls.call_count == 4
        assert spinner_cls.call_args_list[0].args == ("Hashing 1 file(s)...",)
        assert spinner_cls.call_args_list[1].args == ("Uploading 0/1 files · 0.00 GB",)
        assert spinner_cls.call_args_list[2].args == ("Publishing lineage to GLaaS...",)
        assert spinner_cls.call_args_list[3].args == ("Finalizing lineage links...",)

    def test_put_prepared_stores_urls_in_job_metadata(self, tmp_path: Path) -> None:
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        db = _create_mock_db()
        service = PutService(
            db_context=db,
            backend=MemoryBackend(bucket="bucket", prefix="run-1"),
            destination="memory://test-bucket/models",
            repo_root=tmp_path,
            lineage_collector=MagicMock(),
            registration_coordinator=_create_mock_coordinator(),
        )
        service._lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        service.put_prepared(
            prepared=_prepared_put(tmp_path, sources=[model_file]),
            sources=[str(model_file)],
            message="test",
        )

        metadata = json.loads(db.jobs.create.call_args.kwargs["metadata"])
        assert metadata["put"]["message"] == "test"
        assert metadata["put"]["destination"] == "memory://test-bucket/models"
        assert len(metadata["put"]["artifacts"]) == 1

    def test_lineage_membership_index_requires_bloom_fields_for_partial_components(self) -> None:
        with pytest.raises(ValueError, match="missing required bloom fields"):
            build_lineage_membership_index_payload(
                composite_builder=CompositeArtifactBuilder(),
                membership_index=None,
                component_count_total=2,
                components=[
                    {
                        "relative_path": "subset.parquet",
                        "leaf_kind": "file",
                        "component_algorithm": "blake3",
                        "component_digest": "a" * 64,
                        "component_size": 123,
                        "component_type": "application/octet-stream",
                    }
                ],
            )
