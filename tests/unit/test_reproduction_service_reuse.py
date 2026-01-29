"""
Tests for ReproductionService reusing the current repo when remotes match.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.core.interfaces.reproduction import EnvironmentInfo, PipelineInfo
from roar.services.reproduction.service import ReproductionService


def _make_pipeline(**kwargs):
    defaults = dict(
        artifact_hash="abc123",
        git_repo="https://github.com/user/repo.git",
        git_commit="deadbeef",
        build_steps=[],
        run_steps=[],
        total_steps=0,
    )
    defaults.update(kwargs)
    return PipelineInfo(**defaults)


class TestTryReuseCurrentRepo:
    """Tests for _try_reuse_current_repo."""

    def _make_service(self):
        return ReproductionService(glaas_client=None, presenter=MagicMock())

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_reuses_when_remotes_match(self, mock_run, tmp_path):
        """Should return EnvironmentInfo when origin matches pipeline remote."""
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),  # rev-parse
            MagicMock(stdout="git@github.com:user/repo.git\n", returncode=0),  # get-url
            MagicMock(returncode=0),  # checkout
        ]

        svc = self._make_service()
        pipeline = _make_pipeline()

        result = svc._try_reuse_current_repo(tmp_path, pipeline)

        assert result is not None
        assert result.repo_dir == tmp_path

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_returns_none_when_remotes_differ(self, mock_run, tmp_path):
        """Should return None when origin doesn't match."""
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:other/project.git\n", returncode=0),
        ]

        svc = self._make_service()
        pipeline = _make_pipeline()

        result = svc._try_reuse_current_repo(tmp_path, pipeline)
        assert result is None

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_returns_none_when_not_in_git_repo(self, mock_run, tmp_path):
        """Should return None when cwd is not inside a git repo."""
        from subprocess import CalledProcessError

        mock_run.side_effect = CalledProcessError(128, "git")

        svc = self._make_service()
        pipeline = _make_pipeline()

        result = svc._try_reuse_current_repo(tmp_path, pipeline)
        assert result is None

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_detects_venv(self, mock_run, tmp_path):
        """Should set venv_dir when .venv exists."""
        (tmp_path / ".venv").mkdir()
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:user/repo.git\n", returncode=0),
            MagicMock(returncode=0),
        ]

        svc = self._make_service()
        result = svc._try_reuse_current_repo(tmp_path, _make_pipeline())

        assert result is not None
        assert result.venv_dir == tmp_path / ".venv"

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_no_venv_when_missing(self, mock_run, tmp_path):
        """Should set venv_dir to None when .venv doesn't exist."""
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:user/repo.git\n", returncode=0),
            MagicMock(returncode=0),
        ]

        svc = self._make_service()
        result = svc._try_reuse_current_repo(tmp_path, _make_pipeline())

        assert result is not None
        assert result.venv_dir is None

    @patch("roar.services.reproduction.service.subprocess.run")
    def test_returns_none_when_pipeline_has_no_git_repo(self, mock_run, tmp_path):
        """Should return None when pipeline.git_repo is None."""
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:user/repo.git\n", returncode=0),
        ]

        svc = self._make_service()
        pipeline = _make_pipeline(git_repo=None)

        result = svc._try_reuse_current_repo(tmp_path, pipeline)
        assert result is None
