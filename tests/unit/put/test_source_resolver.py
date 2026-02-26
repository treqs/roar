"""
Unit tests for put source resolver.

Tests resolution of various source types to file paths.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from roar.services.put.resolver import SourceResolver


class TestResolveFilePath:
    """Tests for resolving file paths."""

    def test_resolve_existing_file(self, tmp_path: Path):
        """Resolving an existing file returns its path."""
        # Arrange
        test_file = tmp_path / "model.pt"
        test_file.write_text("model data")
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(test_file)])

        # Assert
        assert len(results) == 1
        assert results[0].path == test_file
        assert results[0].exists is True

    def test_resolve_nonexistent_file_raises(self, tmp_path: Path):
        """Resolving a nonexistent file raises an error."""
        resolver = SourceResolver(repo_root=tmp_path)
        nonexistent = tmp_path / "missing.pt"

        with pytest.raises(FileNotFoundError) as exc_info:
            resolver.resolve([str(nonexistent)])

        assert "missing.pt" in str(exc_info.value)

    def test_resolve_multiple_files(self, tmp_path: Path):
        """Resolving multiple files returns all paths."""
        # Arrange
        file1 = tmp_path / "model.pt"
        file2 = tmp_path / "config.yaml"
        file1.write_text("model")
        file2.write_text("config")
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(file1), str(file2)])

        # Assert
        assert len(results) == 2
        paths = {r.path for r in results}
        assert file1 in paths
        assert file2 in paths


class TestResolveDirectory:
    """Tests for resolving directories."""

    def test_resolve_directory_returns_all_files(self, tmp_path: Path):
        """Resolving a directory returns all files within it."""
        # Arrange
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "model.pt").write_text("model")
        (outputs_dir / "metrics.json").write_text("metrics")
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(outputs_dir)])

        # Assert
        assert len(results) == 2
        paths = {r.path for r in results}
        assert outputs_dir / "model.pt" in paths
        assert outputs_dir / "metrics.json" in paths

    def test_resolve_directory_recursive(self, tmp_path: Path):
        """Resolving a directory includes nested subdirectories."""
        # Arrange
        outputs_dir = tmp_path / "outputs"
        checkpoints_dir = outputs_dir / "checkpoints"
        checkpoints_dir.mkdir(parents=True)
        (outputs_dir / "model.pt").write_text("model")
        (checkpoints_dir / "epoch_5.pt").write_text("ckpt5")
        (checkpoints_dir / "epoch_10.pt").write_text("ckpt10")
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(outputs_dir)])

        # Assert
        assert len(results) == 3
        paths = {r.path for r in results}
        assert outputs_dir / "model.pt" in paths
        assert checkpoints_dir / "epoch_5.pt" in paths
        assert checkpoints_dir / "epoch_10.pt" in paths

    def test_resolve_empty_directory_returns_empty(self, tmp_path: Path):
        """Resolving an empty directory returns no results."""
        # Arrange
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(empty_dir)])

        # Assert
        assert len(results) == 0

    def test_directory_files_have_relative_key(self, tmp_path: Path):
        """Files from a directory source should have relative_key set to path within that dir."""
        # Arrange
        checkpoints = tmp_path / "project" / ".nanochat" / "checkpoints"
        checkpoints.mkdir(parents=True)
        (checkpoints / "model.pt").write_text("model")
        (checkpoints / "optimizer.pt").write_text("optimizer")
        subdir = checkpoints / "epoch1"
        subdir.mkdir()
        (subdir / "weights.pt").write_text("weights")

        resolver = SourceResolver(repo_root=tmp_path / "project")

        # Act
        results = resolver.resolve([str(checkpoints)])

        # Assert — relative_key should be relative to the source directory, not repo root
        keys = {r.relative_key for r in results}
        roots = {r.source_root for r in results}
        assert "model.pt" in keys
        assert "optimizer.pt" in keys
        assert "epoch1/weights.pt" in keys
        # Should NOT contain the full repo-relative path
        assert not any(".nanochat" in k for k in keys)
        assert roots == {checkpoints}

    def test_file_source_has_filename_as_relative_key(self, tmp_path: Path):
        """A single file source should have relative_key set to just the filename."""
        # Arrange
        test_file = tmp_path / "deep" / "nested" / "model.pt"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("model data")
        resolver = SourceResolver(repo_root=tmp_path)

        # Act
        results = resolver.resolve([str(test_file)])

        # Assert
        assert len(results) == 1
        assert results[0].relative_key == "model.pt"
        assert results[0].source_root is None


class TestResolveJobReference:
    """Tests for resolving @N job references."""

    def test_resolve_job_reference_returns_outputs(self, tmp_path: Path):
        """Resolving @N returns all outputs from step N."""
        # Arrange
        model_file = tmp_path / "model.pt"
        config_file = tmp_path / "config.yaml"
        model_file.write_text("model")
        config_file.write_text("config")

        # Mock session repository
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_step_by_number.return_value = {"id": 42}

        # Mock job repository
        mock_jobs = Mock()
        mock_jobs.get_outputs.return_value = [
            {"path": str(model_file), "artifact_id": "art1"},
            {"path": str(config_file), "artifact_id": "art2"},
        ]

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
            job_repo=mock_jobs,
        )

        # Act
        results = resolver.resolve(["@2"])

        # Assert
        assert len(results) == 2
        paths = {r.path for r in results}
        assert model_file in paths
        assert config_file in paths
        mock_sessions.get_step_by_number.assert_called_once_with(1, 2)
        mock_jobs.get_outputs.assert_called_once_with(42)

    def test_resolve_job_reference_no_active_session_raises(self, tmp_path: Path):
        """Resolving @N with no active session raises an error."""
        # Arrange
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = None

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            resolver.resolve(["@2"])

        assert "No active session" in str(exc_info.value)

    def test_resolve_job_reference_invalid_step_raises(self, tmp_path: Path):
        """Resolving @N where step N doesn't exist raises an error."""
        # Arrange
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_step_by_number.return_value = None

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            resolver.resolve(["@5"])

        assert "Step 5" in str(exc_info.value)
        assert "not found" in str(exc_info.value)

    def test_resolve_job_reference_skips_composite_outputs_by_default(self, tmp_path: Path):
        """Resolving @N ignores composite outputs unless explicitly enabled."""
        model_file = tmp_path / "model.pt"
        model_file.write_text("model")
        dataset_root = tmp_path / "dataset"
        dataset_root.mkdir()

        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_step_by_number.return_value = {"id": 42}

        mock_jobs = Mock()
        mock_jobs.get_outputs.return_value = [
            {"path": str(model_file), "artifact_id": "art1", "kind": "primitive"},
            {"path": str(dataset_root), "artifact_id": "comp1", "kind": "composite"},
        ]

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
            job_repo=mock_jobs,
        )

        results = resolver.resolve(["@2"])

        assert len(results) == 1
        assert results[0].path == model_file


class TestResolveDefaultSessionOutputs:
    """Tests for resolving default (all session outputs)."""

    def test_resolve_empty_sources_returns_session_outputs(self, tmp_path: Path):
        """Resolving empty sources returns all outputs from active session."""
        # Arrange
        model_file = tmp_path / "model.pt"
        metrics_file = tmp_path / "metrics.json"
        model_file.write_text("model")
        metrics_file.write_text("metrics")

        # Mock session with 2 jobs
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_steps.return_value = [
            {"id": 10, "step_number": 1},
            {"id": 20, "step_number": 2},
        ]

        # Mock job repository
        mock_jobs = Mock()
        mock_jobs.get_outputs.side_effect = [
            [{"path": str(model_file), "artifact_id": "art1"}],  # Job 10
            [{"path": str(metrics_file), "artifact_id": "art2"}],  # Job 20
        ]

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
            job_repo=mock_jobs,
        )

        # Act
        results = resolver.resolve([])

        # Assert
        assert len(results) == 2
        paths = {r.path for r in results}
        assert model_file in paths
        assert metrics_file in paths

    def test_resolve_empty_sources_no_session_raises(self, tmp_path: Path):
        """Resolving empty sources with no active session raises error."""
        # Arrange
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = None

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            resolver.resolve([])

        assert "No active session" in str(exc_info.value)

    def test_resolve_empty_sources_no_outputs_returns_empty(self, tmp_path: Path):
        """Resolving empty sources with session but no outputs returns empty."""
        # Arrange
        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_steps.return_value = []

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
        )

        # Act
        results = resolver.resolve([])

        # Assert
        assert len(results) == 0

    def test_resolve_empty_sources_skips_composite_outputs_by_default(self, tmp_path: Path):
        """Implicit session outputs should skip composite artifacts by default."""
        model_file = tmp_path / "model.pt"
        model_file.write_text("model")
        dataset_root = tmp_path / "dataset"
        dataset_root.mkdir()

        mock_sessions = Mock()
        mock_sessions.get_active.return_value = {"id": 1}
        mock_sessions.get_steps.return_value = [{"id": 10, "step_number": 1}]

        mock_jobs = Mock()
        mock_jobs.get_outputs.return_value = [
            {"path": str(model_file), "artifact_id": "art1", "kind": "primitive"},
            {"path": str(dataset_root), "artifact_id": "comp1", "kind": "composite"},
        ]

        resolver = SourceResolver(
            repo_root=tmp_path,
            session_repo=mock_sessions,
            job_repo=mock_jobs,
        )

        results = resolver.resolve([])

        assert len(results) == 1
        assert results[0].path == model_file
