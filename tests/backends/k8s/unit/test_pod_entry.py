"""Pod-entry contracts: lineage never blocks training, state never shared.

``_run_traced`` shells out to ``roar run`` and cannot see past its exit
code, so it asks for a run report (ROAR_RUN_REPORT_FILE) and reruns the
original command uninstrumented only when roar positively reported a
pre-launch setup failure. A missing report is ambiguous — roar may have
crashed after the workload ran — and must never trigger a rerun.

roar state (db, object-io events, run report) lives in a pod-local
directory keyed by the task identity contract, never in the workdir: a
shared or persistent workdir volume must not share .roar/roar.db across
containers, pods, or retry attempts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from roar.backends.k8s import pod_entry


class _FakeRun:
    """Stub subprocess.run for roar init, roar run, and the fallback."""

    def __init__(self, *, roar_exit: int, report: dict[str, Any] | None) -> None:
        self.roar_exit = roar_exit
        self.report = report
        self.calls: list[list[str]] = []
        self.run_envs: list[dict[str, str]] = []
        self.init_cwds: list[str] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if "roar" in args and "init" in args:
            cwd = Path(str(kwargs.get("cwd") or Path.cwd()))
            self.init_cwds.append(str(cwd))
            (cwd / ".roar").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0)
        if "roar" in args and "run" in args:
            env = dict(kwargs.get("env") or {})
            self.run_envs.append(env)
            target = env.get("ROAR_RUN_REPORT_FILE")
            if target and self.report is not None:
                Path(target).write_text(json.dumps(self.report), encoding="utf-8")
            return subprocess.CompletedProcess(args, self.roar_exit)
        return subprocess.CompletedProcess(args, 7)

    @property
    def fallback_calls(self) -> list[list[str]]:
        return [args for args in self.calls if "roar" not in args]


@pytest.fixture
def pod_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROAR_K8S_WORKDIR", str(workdir))
    monkeypatch.setenv("ROAR_K8S_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROAR_K8S_POD_UID", "pod-uid-1")
    monkeypatch.setenv("ROAR_K8S_CONTAINER", "trainer")
    return workdir


def test_setup_error_report_reruns_uninstrumented(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(roar_exit=1, report={"exit_code": 1, "setup_error": True})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    # The fallback runs the raw command; its exit code (7) is the pod's.
    assert fake.fallback_calls == [["python", "train.py"]]
    assert exit_code == 7


def test_workload_failure_is_propagated_without_rerun(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(roar_exit=3, report={"exit_code": 3, "setup_error": False})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert fake.fallback_calls == []
    assert exit_code == 3


def test_missing_report_never_triggers_rerun(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # roar died without writing a report: ambiguous, the workload may have
    # already run — propagate the failure rather than risk a double-run.
    fake = _FakeRun(roar_exit=1, report=None)
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert fake.fallback_calls == []
    assert exit_code == 1


def test_stale_report_from_prior_attempt_is_ignored(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = pod_entry._run_report_path()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"exit_code": 1, "setup_error": True}), encoding="utf-8")
    fake = _FakeRun(roar_exit=1, report=None)
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert fake.fallback_calls == []
    assert exit_code == 1


def test_successful_run_ignores_report(pod_workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(roar_exit=0, report={"exit_code": 0, "setup_error": False})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    assert pod_entry._run_traced(["python", "train.py"]) == 0
    assert fake.fallback_calls == []


def test_roar_state_is_isolated_from_the_workdir(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(roar_exit=0, report={"exit_code": 0, "setup_error": False})
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    pod_entry._run_traced(["python", "train.py"])

    # init targeted the identity-scoped state dir, not the workdir; the
    # run child was pointed at it via ROAR_PROJECT_DIR.
    state_dir = Path(fake.init_cwds[0])
    assert state_dir == pod_entry._state_root()
    assert "pod-uid-1" in state_dir.name and "trainer" in state_dir.name
    assert fake.run_envs[0]["ROAR_PROJECT_DIR"] == str(state_dir)
    assert not (pod_workdir / ".roar").exists()


def test_state_dirs_differ_per_container_and_attempt(
    pod_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trainer = pod_entry._state_root()
    monkeypatch.setenv("ROAR_K8S_CONTAINER", "sidecar")
    sidecar = pod_entry._state_root()
    monkeypatch.setenv("ROAR_K8S_RESTART_ATTEMPT", "1")
    retry = pod_entry._state_root()

    assert len({trainer, sidecar, retry}) == 3


def test_unusable_state_dir_falls_back_uninstrumented(
    pod_workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("ROAR_K8S_STATE_DIR", str(blocker))
    fake = _FakeRun(roar_exit=0, report=None)
    monkeypatch.setattr(pod_entry.subprocess, "run", fake)

    exit_code = pod_entry._run_traced(["python", "train.py"])

    assert fake.fallback_calls == [["python", "train.py"]]
    assert exit_code == 7
