"""
Unit tests for remote (GLaaS) rendering and reset cleanup of remote sessions.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from roar.presenters.show_renderer import ShowRenderer


class TestShowRendererRemote:
    """Tests for ShowRenderer remote artifact and DAG rendering."""

    def test_render_remote_artifact_basic(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_artifact({
            "hash": "abc123" + "0" * 58,
            "size": 4096,
            "sourceType": "output",
            "sessionHash": "sess456",
            "hashes": [
                {"algorithm": "blake3", "digest": "abc123" + "0" * 58},
            ],
        })
        assert "[remote]" in result
        assert "abc123" in result
        assert "4.0" in result  # format_size may use "4.0KB" or similar
        assert "output" in result
        assert "sess456" in result
        assert "blake3" in result

    def test_render_remote_artifact_minimal(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_artifact({"hash": "deadbeef"})
        assert "[remote]" in result
        assert "deadbeef" in result

    def test_render_remote_artifact_with_created_at_string(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_artifact({
            "hash": "abc123",
            "createdAt": "2025-01-15T10:30:00Z",
        })
        assert "2025-01-15" in result

    def test_render_remote_dag_basic(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_dag({
            "gitRepo": "https://github.com/org/repo",
            "gitCommit": "deadbeef123",
            "jobs": [
                {"stepNumber": 1, "command": "python preprocess.py", "jobType": "run"},
                {"stepNumber": 2, "command": "python train.py", "jobType": "run"},
            ],
        })
        assert "[remote]" in result
        assert "2 step(s)" in result
        assert "github.com/org/repo" in result
        assert "deadbeef123" in result
        assert "python preprocess.py" in result
        assert "python train.py" in result

    def test_render_remote_dag_with_build_steps(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_dag({
            "jobs": [
                {"stepNumber": 1, "command": "make build", "jobType": "build"},
                {"stepNumber": 1, "command": "python train.py", "jobType": "run"},
            ],
        })
        assert "@B" in result
        assert "build" in result

    def test_render_remote_dag_empty_jobs(self):
        renderer = ShowRenderer()
        result = renderer.render_remote_dag({"jobs": []})
        assert "0 step(s)" in result

    def test_render_remote_dag_truncates_long_commands(self):
        renderer = ShowRenderer()
        long_cmd = "python train.py --lr 0.001 --epochs 100 --batch-size 32 --optimizer adam"
        result = renderer.render_remote_dag({
            "jobs": [{"stepNumber": 1, "command": long_cmd, "jobType": "run"}],
        })
        assert "..." in result


class TestResetClearsRemoteSessions:
    """Test that roar reset clears cached remote sessions."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def mock_ctx(self, tmp_path):
        ctx = MagicMock()
        ctx.roar_dir = tmp_path / ".roar"
        ctx.roar_dir.mkdir()
        ctx.cwd = tmp_path
        ctx.is_initialized = True
        return ctx

    def test_reset_clears_remote_sessions(self, runner, mock_ctx):
        import sys
        import roar.cli.commands.reset  # noqa: F401
        from roar.cli.commands.reset import reset

        reset_module = sys.modules["roar.cli.commands.reset"]

        with patch.object(reset_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.sessions.get_active.return_value = {
                "id": 1,
                "hash": "sess123",
            }
            db_ctx.sessions.get_steps.return_value = []
            db_ctx.sessions.create.return_value = 2
            db_ctx.sessions.delete_by_origin.return_value = 3

            result = runner.invoke(reset, ["-y"], obj=mock_ctx)

        assert result.exit_code == 0
        db_ctx.sessions.delete_by_origin.assert_called_once_with("remote")
        assert "Cleared 3 cached remote session(s)" in result.output

    def test_reset_no_remote_sessions_silent(self, runner, mock_ctx):
        import sys
        import roar.cli.commands.reset  # noqa: F401
        from roar.cli.commands.reset import reset

        reset_module = sys.modules["roar.cli.commands.reset"]

        with patch.object(reset_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.sessions.get_active.return_value = {
                "id": 1,
                "hash": "sess123",
            }
            db_ctx.sessions.get_steps.return_value = []
            db_ctx.sessions.create.return_value = 2
            db_ctx.sessions.delete_by_origin.return_value = 0

            result = runner.invoke(reset, ["-y"], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Cleared" not in result.output
