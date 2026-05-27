from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from roar import version_check


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }


def _write_cache(env: dict[str, str], latest: str, checked_at: float | None = None) -> None:
    path = version_check._cache_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"latest": latest, "checked_at": checked_at or time.time()}),
        encoding="utf-8",
    )


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_upgrade_hint_when_newer_available(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_cache(env, "9.9.9")
    with patch("roar.version_check.__version__", "0.3.3"):
        text = version_check.upgrade_hint_text(env)
    assert text is not None
    assert "9.9.9" in text and "0.3.3" in text


def test_no_hint_when_current_is_latest(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_cache(env, "0.3.3")
    with patch("roar.version_check.__version__", "0.3.3"):
        assert version_check.upgrade_hint_text(env) is None


def test_no_hint_when_current_is_newer(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_cache(env, "0.3.3")
    with patch("roar.version_check.__version__", "0.4.0"):
        assert version_check.upgrade_hint_text(env) is None


def test_no_hint_without_cache(tmp_path: Path) -> None:
    assert version_check.upgrade_hint_text(_env(tmp_path)) is None


def test_no_hint_on_corrupt_cache(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = version_check._cache_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert version_check.upgrade_hint_text(env) is None


def test_refresh_writes_cache(tmp_path: Path) -> None:
    env = _env(tmp_path)
    body = json.dumps({"info": {"version": "1.2.3"}}).encode("utf-8")
    with patch("roar.version_check.urllib.request.urlopen", return_value=_Resp(body)):
        version_check.refresh_pypi_version_cache(env, force=True)
    cached = json.loads(version_check._cache_path(env).read_text(encoding="utf-8"))
    assert cached["latest"] == "1.2.3"
    assert "checked_at" in cached


def test_refresh_skips_network_when_cache_fresh(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_cache(env, "0.3.3", checked_at=time.time())  # fresh
    with patch("roar.version_check.urllib.request.urlopen") as urlopen:
        version_check.refresh_pypi_version_cache(env)  # not forced
    urlopen.assert_not_called()


def test_refresh_failopen_on_network_error(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with patch("roar.version_check.urllib.request.urlopen", side_effect=OSError("no net")):
        version_check.refresh_pypi_version_cache(env, force=True)  # must not raise
    assert not version_check._cache_path(env).exists()
