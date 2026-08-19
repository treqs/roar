"""P0-4 end-to-end: when reproduce cannot provision the recorded Python (no uv)
and the running interpreter differs at major.minor, roar warns loudly, recommends
uv, and asks before continuing — with ``--yes`` (auto_confirm) overriding the
prompt. Declining aborts instead of silently reproducing on the wrong Python.

These build a REAL venv via ``python -m venv`` (no subprocess mock) so the whole
non-uv path is exercised, including the actual interpreter-version comparison.
"""

import sys
from unittest.mock import MagicMock

import pytest

from roar.execution.reproduction.environment_setup import EnvironmentSetupService


def _running_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _svc(confirm_return: bool | None = None):
    presenter = MagicMock()
    if confirm_return is not None:
        presenter.confirm.return_value = confirm_return
    svc = EnvironmentSetupService(presenter=presenter)
    svc._use_uv = False  # force the `python -m venv` (running-interpreter) path
    return svc, presenter


def _printed(presenter) -> str:
    return " ".join(str(c.args[0]) for c in presenter.print.call_args_list)


def test_mismatch_declined_aborts(tmp_path):
    """No --yes, user declines the prompt -> RuntimeError, and the warning both
    names the mismatch and points at the uv install docs."""
    svc, presenter = _svc(confirm_return=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RuntimeError, match="aborted"):
        svc._create_venv(repo, "3.99.0", auto_confirm=False)  # 3.99 can't match the runner
    out = _printed(presenter)
    assert "PYTHON VERSION MISMATCH" in out
    assert "docs.astral.sh/uv" in out
    presenter.confirm.assert_called_once()


def test_mismatch_yes_overrides_prompt_and_builds_venv(tmp_path):
    """--yes -> warn loudly but continue without asking; a real venv is built."""
    svc, presenter = _svc()
    repo = tmp_path / "repo"
    repo.mkdir()
    venv = svc._create_venv(repo, "3.99.0", auto_confirm=True)
    assert (venv / "pyvenv.cfg").exists()  # a genuine venv was created
    presenter.confirm.assert_not_called()  # --yes means no prompt
    assert "PYTHON VERSION MISMATCH" in _printed(presenter)


def test_mismatch_confirmed_continues(tmp_path):
    """No --yes, user accepts -> continue and build the venv."""
    svc, presenter = _svc(confirm_return=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    venv = svc._create_venv(repo, "3.99.0", auto_confirm=False)
    assert (venv / "pyvenv.cfg").exists()
    presenter.confirm.assert_called_once()


def test_matching_minor_no_prompt_no_warning(tmp_path):
    """Recorded minor == running minor -> silent: no warning, no prompt, venv built."""
    svc, presenter = _svc(confirm_return=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    venv = svc._create_venv(repo, f"{_running_minor()}.0", auto_confirm=False)
    assert (venv / "pyvenv.cfg").exists()
    presenter.confirm.assert_not_called()
    assert "MISMATCH" not in _printed(presenter)
