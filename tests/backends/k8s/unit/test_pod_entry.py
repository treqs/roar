"""Pod-entry workload-safety contract: lineage never blocks training.

``_run_traced`` shells out to ``roar run`` and cannot see past its exit
code, so it asks for a run report (ROAR_RUN_REPORT_FILE) and reruns the
original command uninstrumented only when roar positively reported a
pre-launch setup failure. A missing report is ambiguous — roar may have
crashed after the workload ran — and must never trigger a rerun.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from roar.backends.k8s import pod_entry


class _FakeRun:
    """Stub subprocess.run capturing invocations of roar run + fallback."""

    def __init__(self, *, roar_exit: int, report: dict[str, Any] | None) -> None:
        self.roar_exit = roar_exit
        self.report = report
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if "roar" in args and "run" in args:
            env = kwargs.get("env") or {}
            target = env.get("ROAR_RUN_REPORT_FILE")
            if target and self.report is not None:
                Path(target).write_text(json.dumps(self.report), encoding="utf-8")
            return subprocess.CompletedProcess(args, self.roar_exit)
        return subprocess.CompletedProcess(args, 7)


@pytest.fixture
def pod_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROAR_K8S_WORKDIR", str(tmp_path))
    (tmp_path / ".roar").mkdir()
    return tmp_path


def test_setup_error_report_reruns_uninstrumented(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(roar_exit=1, report={"exit_code": 1, "setup_error": True})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    # Second call is the raw command; its exit code (7) is the pod's.
    assert len(fake.calls) == 2
    assert fake.calls[1] == ["python", "train.py"]
    assert exit_code == 7


def test_workload_failure_is_propagated_without_rerun(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(roar_exit=3, report={"exit_code": 3, "setup_error": False})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert len(fake.calls) == 1
    assert exit_code == 3


def test_missing_report_never_triggers_rerun(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # roar died without writing a report: ambiguous, the workload may have
    # already run — propagate the failure rather than risk a double-run.
    fake = _FakeRun(roar_exit=1, report=None)
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert len(fake.calls) == 1
    assert exit_code == 1


def test_stale_report_from_prior_attempt_is_ignored(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = pod_workdir / ".roar" / "k8s-run-report.json"
    stale.write_text(json.dumps({"exit_code": 1, "setup_error": True}), encoding="utf-8")
    fake = _FakeRun(roar_exit=1, report=None)
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert len(fake.calls) == 1
    assert exit_code == 1


def test_successful_run_ignores_report(pod_workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(roar_exit=0, report={"exit_code": 0, "setup_error": False})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    assert pod_entry._run_traced(["python", "train.py"]) == 0
    assert len(fake.calls) == 1
