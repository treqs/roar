from __future__ import annotations

from typing import Any

from roar.core.models.run import RunResult
from roar.presenters.run_report import RunReportPresenter


class _CapturePresenter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)

    def print_error(self, message: str) -> None:
        self.messages.append(message)

    def print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        return None

    def print_job(self, job: dict[str, Any], verbose: bool = False) -> None:
        return None

    def print_artifact(self, artifact: dict[str, Any]) -> None:
        return None

    def print_dag(
        self,
        summary: dict[str, Any],
        stale_steps: set[int] | None = None,
    ) -> None:
        return None

    def confirm(self, message: str, default: bool = False) -> bool:
        return default


def test_interrupted_report_references_pop_not_clean() -> None:
    presenter = _CapturePresenter()
    report = RunReportPresenter(presenter)

    report.show_report(
        RunResult(
            exit_code=130,
            job_id=1,
            job_uid="job12345",
            duration=0.5,
            inputs=[],
            outputs=[{"path": "/tmp/out.txt", "size": 1, "hashes": []}],
            interrupted=True,
            is_build=False,
        ),
        ["python", "train.py"],
    )

    rendered = "\n".join(presenter.messages)
    assert "roar pop" in rendered
    assert "roar clean" not in rendered
    assert "roar show --job job12345" in rendered


def test_successful_report_suggests_show_and_dag() -> None:
    presenter = _CapturePresenter()
    report = RunReportPresenter(presenter)

    report.show_report(
        RunResult(
            exit_code=0,
            job_id=2,
            job_uid="job67890",
            duration=1.0,
            inputs=[],
            outputs=[],
            interrupted=False,
            is_build=False,
        ),
        ["python", "train.py"],
    )

    rendered = "\n".join(presenter.messages)
    assert "Next:" in rendered
    assert "roar show --job job67890" in rendered
    assert "roar dag" in rendered
