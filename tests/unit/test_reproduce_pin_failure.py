"""P0-1: reproduce must FAIL (not report success) when a recorded pip pin can't
install, and must offer a debuggable export of the pins."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.execution.reproduction.installers import PythonPackageInstaller


def _installer() -> PythonPackageInstaller:
    return PythonPackageInstaller(use_uv=False, print_fn=lambda *_: None)


def _pip(rc: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stderr=stderr, stdout="")


def _fake_run_pip(*, fail_exact: bool = True, fail_fallback: bool = False, all_ok: bool = False):
    def run_pip(venv_dir, repo_dir, args, show_output=False):
        if all_ok:
            return _pip(0)
        # Exact-pin installs carry "==" and the per-package probe carries "--dry-run";
        # the recovery install of an *unversioned* name is the fallback.
        if any("==" in a for a in args) or "--dry-run" in args:
            return _pip(1 if fail_exact else 0, "No matching distribution")
        return _pip(1 if fail_fallback else 0, "No matching distribution")

    return run_pip


def test_returns_false_when_pin_unresolvable_and_no_any_version(monkeypatch):
    inst = _installer()
    monkeypatch.setattr(inst, "_run_pip", _fake_run_pip(fail_exact=True))
    ok, _warnings = inst.install_packages(
        Path("/venv"),
        ["yanked-pkg==9.9.9"],
        Path("/repo"),
        auto_confirm=True,
        allow_any_version=False,
    )
    assert ok is False  # was True before the fix -> "Environment ready" + dead run


def test_returns_false_when_any_version_fallback_also_fails(monkeypatch):
    inst = _installer()
    monkeypatch.setattr(inst, "_run_pip", _fake_run_pip(fail_exact=True, fail_fallback=True))
    ok, _warnings = inst.install_packages(
        Path("/venv"),
        ["private-pkg==1.0"],
        Path("/repo"),
        auto_confirm=True,
        allow_any_version=True,
    )
    assert ok is False


def test_returns_true_when_all_pins_install(monkeypatch):
    inst = _installer()
    monkeypatch.setattr(inst, "_run_pip", _fake_run_pip(all_ok=True))
    ok, warnings = inst.install_packages(
        Path("/venv"), ["numpy==2.0.0"], Path("/repo"), auto_confirm=True
    )
    assert ok is True
    assert warnings == []


def test_any_version_recovery_returns_true_with_warning(monkeypatch):
    inst = _installer()
    monkeypatch.setattr(inst, "_run_pip", _fake_run_pip(fail_exact=True, fail_fallback=False))
    ok, warnings = inst.install_packages(
        Path("/venv"),
        ["driftable-pkg==1.0"],
        Path("/repo"),
        auto_confirm=True,
        allow_any_version=True,  # the bypass: install an available version
    )
    assert ok is True
    assert any("driftable-pkg" in w for w in warnings)


def test_export_requirements_writes_recorded_pins(tmp_path):
    from roar.application.reproduce.service import _export_pip_requirements

    pipeline = SimpleNamespace(
        build_steps=[],
        run_steps=[{"metadata": {"packages": {"pip": {"numpy": "2.0.0", "torch": "2.7.0"}}}}],
        artifact_hash="abc123def456",
        session_hash=None,
    )
    out = MagicMock()
    dest = tmp_path / "req.txt"
    _export_pip_requirements(pipeline, str(dest), out)

    text = dest.read_text()
    assert "numpy==2.0.0" in text
    assert "torch==2.7.0" in text
    assert text.lstrip().startswith("#")  # has the debug header
