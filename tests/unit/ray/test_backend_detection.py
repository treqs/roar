from __future__ import annotations

from pathlib import Path

import pytest

from roar.ray import worker


@pytest.fixture(autouse=True)
def _reset_worker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_LOG_DIR", "")
    monkeypatch.setattr(worker, "_BACKEND", "filesystem")
    monkeypatch.setattr(worker, "_actor", None)
    monkeypatch.setattr(worker, "_event_buffer", [])


def test_choose_backend_prefers_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker, "_LOG_DIR", str(tmp_path / "logs"))

    monkeypatch.setenv("ROAR_LOG_BACKEND", "actor")
    assert worker._choose_backend() == "actor"

    monkeypatch.setenv("ROAR_LOG_BACKEND", "filesystem")
    assert worker._choose_backend() == "filesystem"


def test_choose_backend_uses_sentinel_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(worker, "_LOG_DIR", str(log_dir))
    monkeypatch.delenv("ROAR_LOG_BACKEND", raising=False)

    assert worker._choose_backend() == "filesystem"
    assert not list(log_dir.glob(".roar-sentinel-*"))


def test_choose_backend_falls_back_to_actor_when_sentinel_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(worker, "_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ROAR_LOG_BACKEND", raising=False)

    def _raise_oserror(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(worker, "_real_open", _raise_oserror)
    assert worker._choose_backend() == "actor"
