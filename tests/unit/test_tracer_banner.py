"""Tests for the per-backend explainer strings.

The banner is no longer emitted from any CLI command — the tradeoff
text now lives in the `roar tracer` status table. We keep `banner_for()`
as a pure data accessor for any future callers (and to verify the text
hasn't drifted from intent).
"""

from __future__ import annotations

from roar.execution.runtime.tracer_banner import banner_for


class TestBannerFor:
    def test_preload_text(self) -> None:
        text = banner_for("preload") or ""
        assert "Selected preload tracer" in text
        assert "CAP_BPF" in text
        assert "shell pipelines" in text

    def test_ptrace_text(self) -> None:
        text = banner_for("ptrace") or ""
        assert "Selected ptrace tracer" in text
        assert "overhead" in text

    def test_ebpf_text(self) -> None:
        text = banner_for("ebpf") or ""
        assert "Selected eBPF tracer" in text
        assert "full" in text.lower()

    def test_unknown_backend(self) -> None:
        assert banner_for("voodoo") is None
