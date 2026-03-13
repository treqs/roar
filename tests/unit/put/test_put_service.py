"""
Unit tests for the put service orchestrator.

Tests the end-to-end put workflow: resolve → upload → create job.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from roar.application.publish.registration import build_lineage_membership_index_payload
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import BatchRegistrationResult, SessionRegistrationResult
from roar.services.put.backends import MemoryBackend
from roar.services.put.composite_builder import CompositeArtifactBuilder
from roar.services.put.service import PutService


def create_mock_glaas_deps():
    """Create mock GLaaS dependencies for testing."""
    mock_client = MagicMock()
    mock_client.health_check.return_value = True
    mock_client.register_composite_artifact.return_value = (
        {"hash": "composite-hash-1", "created": True},
        None,
    )

    mock_session_service = MagicMock()
    mock_session_service.compute_session_hash.return_value = "session_hash_abc123"
    mock_session_service.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session_hash_abc123",
        session_url="https://glaas.ai/dag/session_hash_abc123",
    )

    mock_lineage_collector = MagicMock()
    mock_lineage_collector.collect.return_value = LineageData(
        jobs=[],
        artifacts=[],
        artifact_hashes=set(),
        pipeline={"id": 1},
    )

    mock_coordinator = MagicMock()
    mock_coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=0,
        jobs_failed=0,
        artifacts_registered=0,
        artifacts_failed=0,
        links_created=0,
        links_failed=0,
        errors=[],
    )

    return {
        "glaas_client": mock_client,
        "session_service": mock_session_service,
        "lineage_collector": mock_lineage_collector,
        "registration_coordinator": mock_coordinator,
    }


class TestPutServiceBasic:
    """Tests for basic put service functionality."""

    def test_put_single_file_creates_job(self, tmp_path: Path):
        """Put a single file creates a job record with the file as input."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(
                sources=[str(model_file)],
                message="publish model",
            )

            # Assert
            assert result.success is True
            assert result.job_id == 42
            assert len(result.uploaded_files) == 1
            assert result.uploaded_files[0]["local_path"] == str(model_file)
            assert "memory://test-bucket/models/model.pt" in result.uploaded_files[0]["remote_url"]

            # Verify job was created
            mock_db.jobs.create.assert_called_once()
            call_kwargs = mock_db.jobs.create.call_args[1]
            assert "roar put" in call_kwargs["command"]
            assert call_kwargs["session_id"] == 1
            assert call_kwargs["step_number"] == 3

            # Verify artifact was linked as input
            mock_db.jobs.add_input.assert_called_once()

    def test_put_returns_registered_session_info(self, tmp_path: Path):
        """Put returns the exact session hash/url registered with GLaaS."""
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            result = service.put(
                sources=[str(model_file)],
                message="publish model",
            )

        assert result.success is True
        assert result.session_hash == "session_hash_abc123"
        assert result.session_url == "https://glaas.ai/dag/session_hash_abc123"

    def test_put_registers_uploaded_artifacts_when_lineage_artifacts_are_empty(
        self, tmp_path: Path
    ):
        """Uploaded files must still be registered even when lineage artifacts are empty."""
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(model_file)],
                message="publish model",
            )

        assert result.success is True
        register_call = glaas_deps["registration_coordinator"].register_lineage.call_args
        assert register_call is not None
        artifacts = register_call.kwargs["artifacts"]
        assert len(artifacts) == 1
        assert artifacts[0]["hashes"][0]["algorithm"] == "blake3"
        assert artifacts[0]["session_hash"] == "session_hash_abc123"

    def test_put_stores_urls_in_job_metadata(self, tmp_path: Path):
        """Put stores cloud URLs in job metadata."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="bucket", prefix="run-1")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            service.put(sources=[str(model_file)], message="test")

            # Assert
            call_kwargs = mock_db.jobs.create.call_args[1]
            metadata = json.loads(call_kwargs["metadata"])
            assert "put" in metadata
            assert metadata["put"]["message"] == "test"
            assert "artifacts" in metadata["put"]
            # Should have at least one artifact URL
            assert len(metadata["put"]["artifacts"]) == 1

    def test_put_directory_includes_dataset_identifiers_in_metadata(self, tmp_path: Path):
        """Put on a directory source should emit dataset identifiers in put metadata."""
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        (dataset_dir / "train.csv").write_text("a,b\n1,2\n")
        (dataset_dir / "labels.csv").write_text("id,label\n1,cat\n")

        backend = MemoryBackend(bucket="bucket", prefix="run-1")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            service.put(sources=[str(dataset_dir)], message="publish dataset")

        call_kwargs = mock_db.jobs.create.call_args[1]
        metadata = json.loads(call_kwargs["metadata"])
        dataset_identifiers = metadata["put"]["dataset_identifiers"]
        ids = {item["dataset_id"] for item in dataset_identifiers}
        assert f"file:///{dataset_dir.as_posix().lstrip('/')}" in ids

    def test_put_multiple_files(self, tmp_path: Path):
        """Put multiple files uploads all and links as inputs."""
        # Arrange
        file1 = tmp_path / "model.pt"
        file2 = tmp_path / "config.yaml"
        file1.write_bytes(b"model")
        file2.write_bytes(b"config")

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(sources=[str(file1), str(file2)], message="test")

            # Assert
            assert result.success is True
            assert len(result.uploaded_files) == 2
            assert mock_db.jobs.add_input.call_count == 2

    def test_put_hashes_multiple_files_in_single_batch_call(self, tmp_path: Path):
        """Put computes all source hashes in one batch backend call."""
        import blake3

        file1 = tmp_path / "model.pt"
        file2 = tmp_path / "config.yaml"
        file1.write_bytes(b"model")
        file2.write_bytes(b"config")

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()
        calls = {"count": 0}

        def fake_hashes(paths):
            calls["count"] += 1
            return {str(path): blake3.blake3(Path(path).read_bytes()).hexdigest() for path in paths}

        with (
            patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"),
            patch("roar.services.put.service.hash_files_blake3", side_effect=fake_hashes),
        ):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(sources=[str(file1), str(file2)], message="test")

        assert result.success is True
        assert calls["count"] == 1

    def test_directory_source_registers_composite_artifact(self, tmp_path: Path):
        """Put on a directory source registers a composite artifact in GLaaS."""
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        train_file = dataset_dir / "train.csv"
        labels_file = dataset_dir / "labels.csv"
        train_file.write_text("row1\n")
        labels_file.write_text("label1\n")

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            result = service.put(sources=[str(dataset_dir)], message="publish dataset")

        assert result.success is True
        assert len(result.composites_registered) == 1
        assert result.composites_registered[0]["registered"] is True

        glaas_deps["glaas_client"].register_composite_artifact.assert_called_once()
        payload = glaas_deps["glaas_client"].register_composite_artifact.call_args[0][0]
        assert payload["session_hash"] == "session_hash_abc123"
        assert payload["hashes"][0]["algorithm"] == "composite-blake3"
        assert payload["component_count_total"] == 2
        assert len(payload["components"]) == 2
        assert payload["membership_index"]["total_components"] == 2
        assert payload["membership_index"]["stored_components"] == 2
        assert payload["membership_index"]["bloom_filter_base64"]
        assert payload["membership_index"]["bloom_bits"] > 0
        assert payload["membership_index"]["bloom_hashes"] > 0
        assert payload["membership_index"]["bloom_version"] == 1
        metadata = json.loads(payload["metadata"])
        assert metadata["dataset"]["dataset_id"] == f"file:///{dataset_dir.as_posix().lstrip('/')}"
        assert metadata["dataset"]["dataset_fingerprint"]
        assert metadata["dataset"]["dataset_fingerprint_algorithm"] == "blake3"
        assert metadata["dataset"]["evidence"]
        profile = metadata["dataset"]["profile"]
        assert profile["profile_version"] == 1
        assert profile["total_components"] == 2
        assert profile["profiled_components"] == 2
        assert profile["is_partial"] is False
        assert profile["format_summary"][0]["format"] == "csv"
        assert profile["modality_hint"] == "tabular"
        assert {component["relative_path"] for component in payload["components"]} == {
            "labels.csv",
            "train.csv",
        }

        link_call = glaas_deps["registration_coordinator"].job_service.link_job_artifacts.call_args
        assert link_call is not None
        outputs = link_call.kwargs["outputs"]
        composite_hash = result.composites_registered[0]["hash"]
        assert outputs == [{"artifact_hash": composite_hash, "path": str(dataset_dir)}]

    def test_directory_source_with_symlink_uses_symlink_leaf_digest(self, tmp_path: Path):
        """Composite payload should encode symlink leaves using link-target bytes."""
        import blake3

        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        target_file = dataset_dir / "target.txt"
        target_file.write_text("target content\n")
        alias_file = dataset_dir / "alias.txt"
        alias_file.symlink_to("target.txt")

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(sources=[str(dataset_dir)], message="publish dataset")

        assert result.success is True
        payload = glaas_deps["glaas_client"].register_composite_artifact.call_args[0][0]
        components = {component["relative_path"]: component for component in payload["components"]}
        alias_component = components["alias.txt"]
        target_component = components["target.txt"]

        assert alias_component["leaf_kind"] == "symlink"
        assert alias_component["component_digest"] == blake3.blake3(b"target.txt").hexdigest()
        assert alias_component["component_digest"] != target_component["component_digest"]

    def test_put_links_upstream_lineage_composites_as_inputs(self, tmp_path: Path):
        """Put links upstream composite lineage artifacts as additional put inputs."""
        model_file = tmp_path / "ray_manifest.json"
        model_file.write_text('{"ok": true}\n')
        upstream_root = tmp_path / "data" / "extracted_lance"
        upstream_root.mkdir(parents=True)

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("art-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1
        mock_db.composites = Mock()
        mock_db.composites.get_components.return_value = [
            {
                "relative_path": "part-0000.parquet",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "d" * 64,
                "component_size": 123,
                "component_type": "application/octet-stream",
            }
        ]
        mock_db.composites.get_membership_index.return_value = None

        glaas_deps = create_mock_glaas_deps()
        composite_digest = "c" * 64
        registered_lineage_hashes: set[str] = set()

        def register_composite(payload: dict[str, Any]) -> tuple[dict[str, Any], None]:
            registered_lineage_hashes.add(payload["hash"])
            return {"hash": payload["hash"], "created": True}, None

        glaas_deps["glaas_client"].register_composite_artifact.side_effect = register_composite

        def register_lineage(**_kwargs):
            assert composite_digest in registered_lineage_hashes
            return BatchRegistrationResult(
                session_registered=True,
                jobs_created=0,
                jobs_failed=0,
                artifacts_registered=0,
                artifacts_failed=0,
                links_created=0,
                links_failed=0,
                errors=[],
            )

        glaas_deps["registration_coordinator"].register_lineage.side_effect = register_lineage
        glaas_deps["lineage_collector"].collect.return_value = LineageData(
            jobs=[],
            artifacts=[
                {
                    "id": "local-comp-1",
                    "kind": "composite",
                    "first_seen_path": str(upstream_root),
                    "component_count": 1,
                    "metadata": json.dumps(
                        {
                            "dataset": {
                                "dataset_id": f"file:///{upstream_root.as_posix().lstrip('/')}",
                                "dataset_fingerprint": "f" * 64,
                                "dataset_fingerprint_algorithm": "blake3",
                                "confidence": 0.95,
                                "evidence": ["explicit_root"],
                                "split": "train",
                            }
                        }
                    ),
                    "hashes": [
                        {"algorithm": "composite-blake3", "digest": composite_digest},
                    ],
                }
            ],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        def resolve_artifact_hash(ref: dict) -> tuple[str | None, str | None]:
            if ref.get("hash"):
                return "srv-file-1", None

            hashes = ref.get("hashes")
            if isinstance(hashes, list) and hashes:
                digest = hashes[0].get("digest")
                if digest == composite_digest and composite_digest in registered_lineage_hashes:
                    return "srv-comp-1", None

            return None, "not found"

        glaas_deps[
            "registration_coordinator"
        ].artifact_service.resolve_artifact_hash.side_effect = resolve_artifact_hash

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(sources=[str(model_file)], message="publish model")

        assert result.success is True

        link_call = glaas_deps["registration_coordinator"].job_service.link_job_artifacts.call_args
        assert link_call is not None
        inputs = link_call.kwargs["inputs"]
        assert {"artifact_hash": "srv-file-1", "path": str(model_file)} in inputs
        assert {"artifact_hash": "srv-comp-1", "path": str(upstream_root)} in inputs

        lineage_payload = glaas_deps["glaas_client"].register_composite_artifact.call_args[0][0]
        assert lineage_payload["hash"] == composite_digest
        assert lineage_payload["component_count_total"] == 1
        assert len(lineage_payload["components"]) == 1
        assert lineage_payload["membership_index"]["total_components"] == 1
        assert lineage_payload["membership_index"]["stored_components"] == 1
        assert lineage_payload["membership_index"]["bloom_filter_base64"]
        assert lineage_payload["membership_index"]["bloom_bits"] > 0
        assert lineage_payload["membership_index"]["bloom_hashes"] > 0
        assert lineage_payload["membership_index"]["bloom_version"] == 1
        lineage_metadata = json.loads(lineage_payload["metadata"])
        assert lineage_metadata["dataset"]["dataset_id"] == (
            f"file:///{upstream_root.as_posix().lstrip('/')}"
        )
        assert lineage_metadata["dataset"]["split"] == "train"
        lineage_profile = lineage_metadata["dataset"]["profile"]
        assert lineage_profile["profile_version"] == 1
        assert lineage_profile["total_components"] == 1
        assert lineage_profile["profiled_components"] == 1
        assert lineage_profile["modality_hint"] == "tabular"
        assert lineage_profile["format_summary"][0]["format"] == "parquet"

        mock_db.jobs.add_input.assert_any_call(42, "art-1", str(model_file))
        mock_db.jobs.add_input.assert_any_call(42, "local-comp-1", str(upstream_root))

    def test_lineage_membership_index_requires_bloom_fields_for_partial_components(self):
        """Lineage composites must carry bloom metadata when stored components are partial."""
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

    def test_single_file_source_does_not_register_composite_artifact(self, tmp_path: Path):
        """Put on a single file source should not emit a composite artifact."""
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(sources=[str(model_file)], message="publish model")

        assert result.success is True
        assert result.composites_registered == []

    def test_multi_file_sources_in_same_parent_register_composite_artifact(self, tmp_path: Path):
        """Put on multiple files in one directory should detect and register a composite."""
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        train_file = dataset_dir / "train.csv"
        labels_file = dataset_dir / "labels.csv"
        train_file.write_text("row1\n")
        labels_file.write_text("label1\n")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("artifact-uuid-1", True), ("artifact-uuid-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(train_file), str(labels_file)],
                message="publish grouped files",
            )

        assert result.success is True
        assert len(result.composites_registered) == 1
        composite = result.composites_registered[0]
        assert composite["registered"] is True
        assert composite["root_path"] == str(dataset_dir)

    def test_high_confidence_dataset_identifier_creates_additional_composite_root(
        self, tmp_path: Path
    ):
        """High-confidence dataset id should auto-register a grouped composite root."""
        collection = tmp_path / "collection"
        part_a = collection / "part_a"
        part_b = collection / "part_b"
        part_a.mkdir(parents=True)
        part_b.mkdir(parents=True)
        file_a = part_a / "train.csv"
        file_b = part_b / "labels.csv"
        file_a.write_text("row1\n")
        file_b.write_text("label1\n")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("artifact-uuid-1", True), ("artifact-uuid-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()
        dataset_identifier_inferer = MagicMock()
        dataset_identifier_inferer.infer.return_value = [
            {
                "dataset_id": f"file:///{collection.as_posix().lstrip('/')}",
                "confidence": 0.92,
                "evidence": ["high_cardinality"],
            }
        ]

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                dataset_identifier_inferer=dataset_identifier_inferer,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(file_a), str(file_b)],
                message="publish inferred dataset",
            )

        assert result.success is True
        assert len(result.composites_registered) == 1
        assert result.composites_registered[0]["registered"] is True
        assert result.composites_registered[0]["root_path"] == str(collection)
        assert dataset_identifier_inferer.infer.call_count == 1

    def test_unexpected_composite_registration_response_marks_put_unsuccessful(
        self, tmp_path: Path
    ):
        """Unexpected GLaaS composite response should fail registration status."""
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        train_file = dataset_dir / "train.csv"
        labels_file = dataset_dir / "labels.csv"
        train_file.write_text("row1\n")
        labels_file.write_text("label1\n")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("artifact-uuid-1", True), ("artifact-uuid-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()
        glaas_deps["glaas_client"].register_composite_artifact.return_value = object()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(train_file), str(labels_file)],
                message="publish grouped files",
            )

        assert result.success is False
        assert len(result.composites_registered) == 1
        assert result.composites_registered[0]["registered"] is False
        assert "error" in result.composites_registered[0]
        assert result.error is not None
        assert "Composite" in result.error

    def test_unexpected_composite_registration_tuple_payload_marks_put_unsuccessful(
        self, tmp_path: Path
    ):
        """Tuple responses with non-dict payload should fail registration status."""
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        train_file = dataset_dir / "train.csv"
        labels_file = dataset_dir / "labels.csv"
        train_file.write_text("row1\n")
        labels_file.write_text("label1\n")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("artifact-uuid-1", True), ("artifact-uuid-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()
        glaas_deps["glaas_client"].register_composite_artifact.return_value = ("ok", None)

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(train_file), str(labels_file)],
                message="publish grouped files",
            )

        assert result.success is False
        assert len(result.composites_registered) == 1
        assert result.composites_registered[0]["registered"] is False
        assert "expected dict payload" in result.composites_registered[0]["error"]
        assert result.error is not None
        assert "Composite" in result.error

    def test_medium_confidence_dataset_identifier_does_not_auto_register_composite_root(
        self, tmp_path: Path
    ):
        """Medium-confidence dataset id should be metadata-only, not auto composite."""
        collection = tmp_path / "collection"
        part_a = collection / "part_a"
        part_b = collection / "part_b"
        part_a.mkdir(parents=True)
        part_b.mkdir(parents=True)
        file_a = part_a / "train.csv"
        file_b = part_b / "labels.csv"
        file_a.write_text("row1\n")
        file_b.write_text("label1\n")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("artifact-uuid-1", True), ("artifact-uuid-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()
        dataset_identifier_inferer = MagicMock()
        dataset_identifier_inferer.infer.return_value = [
            {
                "dataset_id": f"file:///{collection.as_posix().lstrip('/')}",
                "confidence": 0.72,
                "evidence": ["high_cardinality"],
            }
        ]

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                dataset_identifier_inferer=dataset_identifier_inferer,
                **glaas_deps,
            )
            result = service.put(
                sources=[str(file_a), str(file_b)],
                message="publish inferred dataset",
            )

        assert result.success is True
        assert result.composites_registered == []
        assert dataset_identifier_inferer.infer.call_count == 1


class TestPutServiceDryRun:
    """Tests for dry run mode."""

    def test_dry_run_does_not_upload(self, tmp_path: Path):
        """Dry run doesn't upload files or create job."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(
                sources=[str(model_file)],
                message="test",
                dry_run=True,
            )

            # Assert
            assert result.success is True
            assert result.dry_run is True
            assert result.job_id is None
            assert len(result.would_upload) == 1
            assert backend.exists("model.pt") is False
            mock_db.jobs.create.assert_not_called()


class TestPutServiceErrors:
    """Tests for error handling."""

    def test_put_no_active_session_raises(self, tmp_path: Path):
        """Put without active session raises error."""
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"data")

        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = None

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            with pytest.raises(ValueError, match=r"No active session"):
                service.put(sources=[str(model_file)], message="test")

    def test_put_missing_file_raises(self, tmp_path: Path):
        """Put with missing file raises FileNotFoundError."""
        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            with pytest.raises(FileNotFoundError):
                service.put(sources=[str(tmp_path / "missing.pt")], message="test")
