"""Application tests for log-query orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import roar.application.query.log as log_module
from roar.application.query import LogQueryRequest, render_log
from roar.application.query.results import LogSummary


def _request(tmp_path: Path, *, use_color: bool = False) -> LogQueryRequest:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    return LogQueryRequest(roar_dir=roar_dir, use_color=use_color)


def test_build_log_summary_returns_typed_jobs_in_display_order(tmp_path: Path) -> None:
    with patch.object(log_module, "create_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = {"id": 11}
        db_ctx.jobs.get_by_session.return_value = [
            {
                "job_uid": "job00002",
                "step_number": 2,
                "job_type": None,
                "timestamp": 1700000200.0,
                "duration_seconds": 4.2,
                "exit_code": 0,
                "command": "python train.py",
            },
            {
                "job_uid": "job00001",
                "step_number": 1,
                "job_type": None,
                "timestamp": 1700000100.0,
                "duration_seconds": 1.5,
                "exit_code": 0,
                "command": "python preprocess.py",
            },
        ]

        summary = log_module.build_log_summary(_request(tmp_path))

    assert isinstance(summary, LogSummary)
    assert [job.job_uid for job in summary.jobs] == ["job00001", "job00002"]
    assert summary.jobs[0].command == "python preprocess.py"


def test_render_log_without_active_session_returns_message(tmp_path: Path) -> None:
    with patch.object(log_module, "create_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = None

        rendered = render_log(_request(tmp_path))

    assert rendered == "No active session."
