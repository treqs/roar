"""Tests for reproduction environment helpers."""

from unittest.mock import MagicMock, patch

from roar.application.reproduce.environment import (
    prepare_reproduction_environment,
    try_reuse_current_repo,
)
from roar.core.interfaces.reproduction import PipelineInfo


def _make_pipeline(**kwargs):
    defaults = {
        "artifact_hash": "abc123",
        "git_repo": "https://github.com/user/repo.git",
        "git_commit": "deadbeef",
        "build_steps": [],
        "run_steps": [],
        "total_steps": 0,
    }
    defaults.update(kwargs)
    return PipelineInfo(**defaults)


class TestTryReuseCurrentRepo:
    @patch("roar.application.reproduce.environment.subprocess.run")
    def test_reuses_when_remotes_match(self, mock_run, tmp_path):
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:user/repo.git\n", returncode=0),
            MagicMock(returncode=0),
        ]

        result = try_reuse_current_repo(tmp_path, _make_pipeline(), presenter=MagicMock())

        assert result is not None
        assert result.repo_dir == tmp_path

    @patch("roar.application.reproduce.environment.subprocess.run")
    def test_returns_none_when_remotes_differ(self, mock_run, tmp_path):
        mock_run.side_effect = [
            MagicMock(stdout=str(tmp_path) + "\n", returncode=0),
            MagicMock(stdout="git@github.com:other/project.git\n", returncode=0),
        ]

        result = try_reuse_current_repo(tmp_path, _make_pipeline(), presenter=MagicMock())
        assert result is None


class TestPrepareReproductionEnvironment:
    @patch("roar.application.reproduce.environment.EnvironmentSetupService")
    @patch("roar.application.reproduce.environment.try_reuse_current_repo")
    def test_can_skip_current_repo_reuse(self, mock_reuse, mock_setup_cls, tmp_path):
        setup = mock_setup_cls.return_value
        setup.setup.return_value = MagicMock()

        prepare_reproduction_environment(
            pipeline=_make_pipeline(),
            cwd=tmp_path,
            presenter=MagicMock(),
            auto_confirm=True,
            reuse_current_repo=False,
        )

        mock_reuse.assert_not_called()
        setup.setup.assert_called_once()
