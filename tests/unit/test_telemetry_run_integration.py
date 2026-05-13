from __future__ import annotations

from types import SimpleNamespace

import pytest

from roar.application.run import service
from roar.application.run.execution import ExecutionReport


def test_run_telemetry_records_after_execution_finalizer(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        service,
        "plan_execution_command",
        lambda command: SimpleNamespace(
            backend_name="local",
            execution_role="driver",
            command=command,
            finalize_run=lambda _ctx: calls.append("finalize"),
        ),
    )
    monkeypatch.setattr(
        service,
        "execute_and_report",
        lambda **_kwargs: (
            calls.append("execute") or ExecutionReport(exit_code=0, tracer_backend="ptrace")
        ),
    )

    def record_run_outcome(**kwargs: object) -> None:
        calls.append(("telemetry", kwargs))

    monkeypatch.setattr("roar.telemetry.hooks.record_run_outcome", record_run_outcome)

    exit_code = service._execute_tracked_command(
        roar_dir=tmp_path / ".roar",
        repo_root=str(tmp_path),
        command=["python", "train.py"],
        job_type=None,
        step_name=None,
        verbosity="normal",
        hash_algorithms=["blake3"],
        tracer_mode=None,
        tracer_fallback=None,
    )

    assert exit_code == 0
    assert calls[0] == "execute"
    assert calls[1] == "finalize"
    assert calls[2] == (
        "telemetry",
        {
            "success": True,
            "failure_kind": None,
            "tracer_backend": "ptrace",
            "start_dir": str(tmp_path),
        },
    )


def test_build_execution_does_not_record_run_outcome_telemetry(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        service,
        "plan_execution_command",
        lambda command: SimpleNamespace(
            backend_name="local",
            execution_role="driver",
            command=command,
            finalize_run=lambda _ctx: calls.append("finalize"),
        ),
    )
    monkeypatch.setattr(
        service,
        "execute_and_report",
        lambda **_kwargs: calls.append("execute") or ExecutionReport(exit_code=0),
    )
    monkeypatch.setattr(
        "roar.telemetry.hooks.record_run_outcome",
        lambda **_kwargs: calls.append("telemetry"),
    )

    exit_code = service._execute_tracked_command(
        roar_dir=tmp_path / ".roar",
        repo_root=str(tmp_path),
        command=["python", "prep.py"],
        job_type="build",
        step_name=None,
        verbosity="normal",
        hash_algorithms=["blake3"],
        tracer_mode=None,
        tracer_fallback=None,
    )

    assert exit_code == 0
    assert calls == ["execute", "finalize"]


def test_finalizer_failure_records_internal_run_failure(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    def fail_finalizer(_ctx: object) -> None:
        calls.append("finalize")
        raise RuntimeError("finalizer failed")

    monkeypatch.setattr(
        service,
        "plan_execution_command",
        lambda command: SimpleNamespace(
            backend_name="local",
            execution_role="driver",
            command=command,
            finalize_run=fail_finalizer,
        ),
    )
    monkeypatch.setattr(
        service,
        "execute_and_report",
        lambda **_kwargs: (
            calls.append("execute") or ExecutionReport(exit_code=0, tracer_backend="preload")
        ),
    )

    def record_run_outcome(**kwargs: object) -> None:
        calls.append(("telemetry", kwargs))

    monkeypatch.setattr("roar.telemetry.hooks.record_run_outcome", record_run_outcome)

    with pytest.raises(RuntimeError, match="finalizer failed"):
        service._execute_tracked_command(
            roar_dir=tmp_path / ".roar",
            repo_root=str(tmp_path),
            command=["python", "train.py"],
            job_type=None,
            step_name=None,
            verbosity="normal",
            hash_algorithms=["blake3"],
            tracer_mode=None,
            tracer_fallback=None,
        )

    assert calls == [
        "execute",
        "finalize",
        (
            "telemetry",
            {
                "success": False,
                "failure_kind": "internal",
                "tracer_backend": "preload",
                "start_dir": str(tmp_path),
            },
        ),
    ]


def test_pre_report_execution_failure_records_internal_run_failure(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        service,
        "plan_execution_command",
        lambda command: SimpleNamespace(
            backend_name="local",
            execution_role="driver",
            command=command,
            finalize_run=lambda _ctx: calls.append("finalize"),
        ),
    )

    def fail_before_report(**_kwargs: object) -> ExecutionReport:
        calls.append("execute")
        raise RuntimeError("execution setup failed")

    monkeypatch.setattr(service, "execute_and_report", fail_before_report)

    def record_run_outcome(**kwargs: object) -> None:
        calls.append(("telemetry", kwargs))

    monkeypatch.setattr("roar.telemetry.hooks.record_run_outcome", record_run_outcome)

    with pytest.raises(RuntimeError, match="execution setup failed"):
        service._execute_tracked_command(
            roar_dir=tmp_path / ".roar",
            repo_root=str(tmp_path),
            command=["python", "train.py"],
            job_type=None,
            step_name=None,
            verbosity="normal",
            hash_algorithms=["blake3"],
            tracer_mode=None,
            tracer_fallback=None,
        )

    assert calls == [
        "execute",
        (
            "telemetry",
            {
                "success": False,
                "failure_kind": "internal",
                "tracer_backend": None,
                "start_dir": str(tmp_path),
            },
        ),
    ]
