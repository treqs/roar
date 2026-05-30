from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from roar.execution.runtime import lazy_install


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the runtime cache to a tmp dir for all tests in this module."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "roar" / "runtime"


# ---------------------------------------------------------------------------
# Cache layout
# ---------------------------------------------------------------------------


def test_runtime_cache_dir_respects_xdg(cache_root: Path) -> None:
    assert lazy_install.runtime_cache_dir("cp312") == cache_root / "cp312"
    assert lazy_install.runtime_site_packages("cp312") == cache_root / "cp312" / "site-packages"


def test_runtime_cache_root_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert lazy_install.runtime_cache_root() == tmp_path / ".cache" / "roar" / "runtime"


# ---------------------------------------------------------------------------
# is_runtime_cached
# ---------------------------------------------------------------------------


def test_is_runtime_cached_returns_false_without_stamp(cache_root: Path) -> None:
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is False


def test_is_runtime_cached_returns_true_for_matching_version(cache_root: Path) -> None:
    abi_dir = cache_root / "cp312"
    abi_dir.mkdir(parents=True)
    (abi_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": "0.3.0"}))
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is True


def test_is_runtime_cached_returns_false_for_stale_version(cache_root: Path) -> None:
    abi_dir = cache_root / "cp312"
    abi_dir.mkdir(parents=True)
    (abi_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": "0.2.0"}))
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is False


def test_is_runtime_cached_handles_corrupt_stamp(cache_root: Path) -> None:
    abi_dir = cache_root / "cp312"
    abi_dir.mkdir(parents=True)
    (abi_dir / "roar_runtime.json").write_text("not json")
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is False


# ---------------------------------------------------------------------------
# install_runtime — subprocess mocked, real tempdir / rename behavior
# ---------------------------------------------------------------------------


def test_install_runtime_writes_stamp_and_returns_true_on_success(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend uv is on PATH so _select_installer picks it.
    monkeypatch.setattr(
        lazy_install.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    # Mock the subprocess.run that does the install — pretend it succeeded.
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr="")),
    )

    success = lazy_install.install_runtime("cp312", "/usr/bin/python3.12", "0.3.0")

    assert success is True
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is True
    stamp = json.loads((cache_root / "cp312" / "roar_runtime.json").read_text())
    assert stamp["roar_version"] == "0.3.0"
    assert stamp["abi_tag"] == "cp312"


def test_install_runtime_returns_false_on_subprocess_failure(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lazy_install.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="no network")),
    )

    success = lazy_install.install_runtime("cp312", "/usr/bin/python3.12", "0.3.0")

    assert success is False
    assert not (cache_root / "cp312").exists()


def test_install_runtime_returns_false_when_no_installer_available(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lazy_install.shutil, "which", lambda _name: None)
    spy = MagicMock()
    monkeypatch.setattr(subprocess, "run", spy)

    assert lazy_install.install_runtime("cp312", "/usr/bin/python3.12", "0.3.0") is False
    assert spy.call_count == 0


def test_install_runtime_replaces_stale_cache(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-existing stale tree.
    stale_dir = cache_root / "cp312"
    stale_dir.mkdir(parents=True)
    (stale_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": "0.2.0"}))
    (stale_dir / "site-packages").mkdir()
    (stale_dir / "site-packages" / "stale.txt").write_text("old")

    monkeypatch.setattr(
        lazy_install.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr="")),
    )

    assert lazy_install.install_runtime("cp312", "/usr/bin/python3.12", "0.3.0") is True
    assert not (stale_dir / "site-packages" / "stale.txt").exists()
    assert lazy_install.is_runtime_cached("cp312", "0.3.0") is True


# ---------------------------------------------------------------------------
# runtime_install_mode
# ---------------------------------------------------------------------------


def test_runtime_install_mode_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROAR_RUNTIME_INSTALL", raising=False)
    monkeypatch.setattr(
        "roar.integrations.config.access.config_get",
        lambda _key, **_kwargs: None,
    )
    assert lazy_install.runtime_install_mode() == "auto"


def test_runtime_install_mode_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROAR_RUNTIME_INSTALL", "skip")
    monkeypatch.setattr(
        "roar.integrations.config.access.config_get",
        lambda _key, **_kwargs: "auto",
    )
    assert lazy_install.runtime_install_mode() == "skip"


def test_runtime_install_mode_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROAR_RUNTIME_INSTALL", raising=False)
    monkeypatch.setattr(
        "roar.integrations.config.access.config_get",
        lambda _key, **_kwargs: "skip",
    )
    assert lazy_install.runtime_install_mode() == "skip"


def test_runtime_install_mode_normalizes_case_and_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROAR_RUNTIME_INSTALL", "SKIP")
    assert lazy_install.runtime_install_mode() == "skip"
    monkeypatch.setenv("ROAR_RUNTIME_INSTALL", "nonsense")
    assert lazy_install.runtime_install_mode() == "auto"


# ---------------------------------------------------------------------------
# ensure_runtime — the orchestration decision tree
# ---------------------------------------------------------------------------


def test_ensure_runtime_returns_none_when_abi_matches_bundled(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(lazy_install, "install_runtime", spy)
    result = lazy_install.ensure_runtime(
        target_python="/usr/bin/python3.13",
        target_abi="cp313",
        bundled_abi="cp313",
        roar_version="0.3.0",
    )
    assert result is None
    assert spy.call_count == 0


def test_ensure_runtime_returns_none_when_mode_is_skip(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(lazy_install, "install_runtime", spy)
    result = lazy_install.ensure_runtime(
        target_python="/usr/bin/python3.12",
        target_abi="cp312",
        bundled_abi="cp313",
        roar_version="0.3.0",
        mode="skip",
    )
    assert result is None
    assert spy.call_count == 0


def test_ensure_runtime_returns_cache_path_on_cache_hit(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    abi_dir = cache_root / "cp312"
    abi_dir.mkdir(parents=True)
    (abi_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": "0.3.0"}))
    spy = MagicMock()
    monkeypatch.setattr(lazy_install, "install_runtime", spy)

    result = lazy_install.ensure_runtime(
        target_python="/usr/bin/python3.12",
        target_abi="cp312",
        bundled_abi="cp313",
        roar_version="0.3.0",
        mode="auto",
    )
    assert result == abi_dir / "site-packages"
    assert spy.call_count == 0


def test_ensure_runtime_triggers_install_on_cache_miss(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_install(*_args, **_kwargs) -> bool:
        abi_dir = cache_root / "cp312"
        abi_dir.mkdir(parents=True)
        (abi_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": "0.3.0"}))
        return True

    monkeypatch.setattr(lazy_install, "install_runtime", fake_install)
    result = lazy_install.ensure_runtime(
        target_python="/usr/bin/python3.12",
        target_abi="cp312",
        bundled_abi="cp313",
        roar_version="0.3.0",
        mode="auto",
    )
    assert result == cache_root / "cp312" / "site-packages"


def test_ensure_runtime_returns_none_on_install_failure(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lazy_install, "install_runtime", lambda *_a, **_kw: False)
    result = lazy_install.ensure_runtime(
        target_python="/usr/bin/python3.12",
        target_abi="cp312",
        bundled_abi="cp313",
        roar_version="0.3.0",
        mode="auto",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Concurrency — the torchrun "thundering herd" (one worker per GPU, all hitting
# a cold cache at once must collapse to a single install, not N).
# ---------------------------------------------------------------------------


def test_ensure_runtime_serializes_concurrent_installs(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_calls: list[str] = []
    install_calls_lock = threading.Lock()

    def fake_install(abi_tag: str, target_python: str, roar_version: str, *_a, **_kw) -> bool:
        with install_calls_lock:
            install_calls.append(abi_tag)
        # Widen the race window so a missing lock reliably lets every worker in.
        time.sleep(0.2)
        abi_dir = cache_root / abi_tag
        abi_dir.mkdir(parents=True, exist_ok=True)
        (abi_dir / "site-packages").mkdir(exist_ok=True)
        (abi_dir / "roar_runtime.json").write_text(json.dumps({"roar_version": roar_version}))
        return True

    monkeypatch.setattr(lazy_install, "install_runtime", fake_install)

    workers = 8
    start = threading.Barrier(workers)
    results: list[Path | None] = [None] * workers

    def worker(idx: int) -> None:
        start.wait()  # release all workers into ensure_runtime simultaneously
        results[idx] = lazy_install.ensure_runtime(
            target_python="/usr/bin/python3.10",
            target_abi="cp310",
            bundled_abi="cp313",
            roar_version="0.3.0",
            mode="auto",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The lock winner installs once; the other 7 re-check the cache and reuse it.
    assert install_calls == ["cp310"]
    expected = cache_root / "cp310" / "site-packages"
    assert all(r == expected for r in results)


# ---------------------------------------------------------------------------
# Installer subprocess env — must not re-inject roar into itself when called
# from inside a traced process (the in-process repair path).
# ---------------------------------------------------------------------------


def test_clean_subprocess_env_strips_roar_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    inject_dir = str(Path(lazy_install.__file__).resolve().parent / "inject")
    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv("ROAR_RUNTIME_PYTHONPATH", "/cache/cp310/site-packages")
    monkeypatch.setenv("ROAR_RUNTIME_PYTHONPATH_ACTIVE", "/cache/cp310/site-packages")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([inject_dir, "/keep/me"]))
    monkeypatch.setenv("PATH", "/usr/bin")  # unrelated vars survive

    env = lazy_install._clean_subprocess_env()

    assert "ROAR_WRAP" not in env
    assert "ROAR_RUNTIME_PYTHONPATH" not in env
    assert "ROAR_RUNTIME_PYTHONPATH_ACTIVE" not in env
    assert env["PYTHONPATH"] == "/keep/me"  # inject dir dropped, rest kept
    assert env["PATH"] == "/usr/bin"


def test_clean_subprocess_env_drops_pythonpath_when_only_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inject_dir = str(Path(lazy_install.__file__).resolve().parent / "inject")
    monkeypatch.setenv("PYTHONPATH", inject_dir)

    env = lazy_install._clean_subprocess_env()

    assert "PYTHONPATH" not in env
