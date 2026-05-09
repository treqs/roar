"""Tests for the tracer-selection banner (P0-7).

Covers:
  - First-time per-machine: banner shown once, then state persists
  - Fallback (auto mode picking non-eBPF): banner shown on every run
  - Explicit `--tracer X` (user-chose) does not trigger fallback banner
  - `tracer.banner = false` suppresses everything
  - State-file write failures degrade silently
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from roar.execution.runtime import tracer_banner


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the banner module at a per-test state directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "roar"


@pytest.fixture
def banner_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracer_banner, "_config_banner_enabled", lambda: True)


@pytest.fixture
def banner_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracer_banner, "_config_banner_enabled", lambda: False)


def _emit(backend: str, mode: str | None) -> tuple[bool, str]:
    out = io.StringIO()
    fired = tracer_banner.emit_banner_if_needed(backend, mode, stream=out)
    return fired, out.getvalue()


class TestFirstTimeBackend:
    def test_first_use_emits_banner_and_marks_seen(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        fired, text = _emit("ebpf", None)
        assert fired
        assert "eBPF tracer" in text

        state_file = isolated_state / "tracer_banners.json"
        assert state_file.exists()
        seen = json.loads(state_file.read_text())["seen_backends"]
        assert seen == ["ebpf"]

    def test_second_use_same_backend_silent_when_not_fallback(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        # User explicitly picked ptrace, sees banner once.
        fired1, _ = _emit("ptrace", "ptrace")
        # Same explicit choice next run — already seen, not fallback.
        fired2, text2 = _emit("ptrace", "ptrace")
        assert fired1
        assert not fired2
        assert text2 == ""


class TestFallback:
    def test_auto_picking_preload_is_fallback_and_banners(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        fired, text = _emit("preload", None)
        assert fired
        assert "preload tracer" in text
        assert "CAP_BPF" in text  # mentions the upgrade path
        assert "setcap" in text

    def test_auto_picking_ptrace_is_fallback_and_banners(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        fired, text = _emit("ptrace", None)
        assert fired
        assert "ptrace tracer" in text

    def test_auto_picking_ebpf_is_not_fallback(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        # First-time still fires the banner; the "fallback" trigger should NOT.
        # Mark seen first to isolate the fallback logic.
        tracer_banner._save_seen({"ebpf"})
        fired, text = _emit("ebpf", None)
        assert not fired
        assert text == ""

    def test_fallback_fires_repeatedly_after_first_seen(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        """Fallback is a real degradation; the user should be reminded
        every time it happens, not just once."""
        fired1, _ = _emit("preload", None)
        fired2, _ = _emit("preload", None)
        fired3, _ = _emit("preload", "auto")
        assert fired1
        assert fired2
        assert fired3

    def test_explicit_choice_after_seen_does_not_banner(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        """If the user explicitly says `--tracer preload`, they're aware
        of the choice. After the first banner, don't keep nagging."""
        fired1, _ = _emit("preload", "preload")
        fired2, _ = _emit("preload", "preload")
        assert fired1
        assert not fired2


class TestSuppression:
    def test_config_disabled_suppresses_first_time(
        self, isolated_state: Path, banner_disabled: None
    ) -> None:
        fired, text = _emit("ebpf", None)
        assert not fired
        assert text == ""

    def test_config_disabled_suppresses_fallback(
        self, isolated_state: Path, banner_disabled: None
    ) -> None:
        fired, text = _emit("preload", None)
        assert not fired
        assert text == ""


class TestStateFileResilience:
    def test_unwritable_state_dir_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, banner_enabled: None
    ) -> None:
        """If $XDG_STATE_HOME points somewhere we can't write (read-only
        filesystem, container, etc.), the banner should still print and
        the run shouldn't fail."""
        monkeypatch.setenv("XDG_STATE_HOME", "/proc/0/cannot-write")
        fired, text = _emit("preload", None)
        assert fired
        assert text  # something printed

    def test_corrupt_state_file_falls_back_to_empty_seen(
        self, isolated_state: Path, banner_enabled: None
    ) -> None:
        state_file = isolated_state / "tracer_banners.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("not json {{{ broken")

        # Treat as no backends seen — first-use trigger fires.
        fired, _ = _emit("ebpf", "ebpf")
        assert fired


class TestBackendSpecificMessages:
    @pytest.mark.parametrize(
        "backend,must_contain",
        [
            ("preload", "CAP_BPF"),
            ("preload", "setcap"),
            ("preload", "shell pipelines"),
            ("ptrace", "preload not available"),
            ("ebpf", "full coverage"),
        ],
    )
    def test_banner_text(
        self,
        isolated_state: Path,
        banner_enabled: None,
        backend: str,
        must_contain: str,
    ) -> None:
        _, text = _emit(backend, None)
        assert must_contain in text


class TestUnknownBackend:
    def test_unknown_backend_no_banner(self, isolated_state: Path, banner_enabled: None) -> None:
        fired, text = _emit("voodoo", None)
        assert not fired
        assert text == ""


class TestConfigIntegration:
    def test_real_config_default_is_banner_enabled(self, isolated_state: Path) -> None:
        """Spot-check the default-true config knob path. We don't mock
        _config_banner_enabled here — runs the real config_get."""
        with patch("roar.execution.runtime.tracer_banner.config_get", return_value=None):
            assert tracer_banner._config_banner_enabled() is True

    def test_real_config_explicit_false_suppresses(self, isolated_state: Path) -> None:
        with patch("roar.execution.runtime.tracer_banner.config_get", return_value=False):
            assert tracer_banner._config_banner_enabled() is False
