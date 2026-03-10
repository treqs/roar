"""Unit tests: _merge_roar_runtime_env_pip() must not produce duplicate pip entries.

BUG: when ROAR_CLUSTER_PIP_REQ is a URL-based requirement (e.g. a presigned S3 URL),
_requirement_name() returns the full URL rather than a canonical package name like
'roar-cli'. The deduplication filter checks for names in {'roar-cli', 'roar'} — which
never matches a URL — so the URL survives the filter AND gets appended again.

These tests drive the fix directly via the internal function.
"""

from __future__ import annotations

import pytest

from roar.cli.commands._ray_job_submit import _merge_roar_runtime_env_pip

FAKE_WHEEL_URL = (
    "https://example.com/wheels/roar_cli-0.2.12-cp312-cp312-linux_x86_64.whl"
    "?X-Amz-Signature=deadbeef"
)


class TestMergeRoarRuntimeEnvPipDedup:
    """_merge_roar_runtime_env_pip must deduplicate URL-based wheel requirements."""

    def test_url_req_not_duplicated_when_already_in_pip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If pip already contains the ROAR_CLUSTER_PIP_REQ URL, it must not appear twice."""
        monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", FAKE_WHEEL_URL)

        # Simulate a runtime_env that already has the wheel URL (e.g. user passed it in).
        existing_pip = [FAKE_WHEEL_URL]
        result = _merge_roar_runtime_env_pip(existing_pip)

        assert result is not None
        wheel_entries = [r for r in result if "roar_cli" in r or "roar-cli" in r]
        assert len(wheel_entries) == 1, (
            f"Expected exactly 1 roar wheel entry, got {len(wheel_entries)}: {wheel_entries}\n"
            f"Full result: {result}"
        )

    def test_url_req_appears_exactly_once_with_empty_pip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROAR_CLUSTER_PIP_REQ URL should appear exactly once when pip starts empty."""
        monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", FAKE_WHEEL_URL)

        result = _merge_roar_runtime_env_pip(None)

        assert result is not None
        assert result.count(FAKE_WHEEL_URL) == 1, (
            f"Expected URL to appear once, got {result.count(FAKE_WHEEL_URL)}: {result}"
        )

    def test_url_req_replaces_existing_roar_cli_version_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URL override should replace an existing roar-cli==x.y.z pin, not join it."""
        monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", FAKE_WHEEL_URL)

        existing_pip = ["roar-cli==0.2.11", "numpy>=1.0"]
        result = _merge_roar_runtime_env_pip(existing_pip)

        assert result is not None
        roar_entries = [r for r in result if "roar" in r.lower()]
        assert len(roar_entries) == 1, (
            f"Expected exactly 1 roar entry after replacing version pin, "
            f"got {roar_entries}: {result}"
        )
        assert roar_entries[0] == FAKE_WHEEL_URL

    def test_skip_produces_no_roar_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ROAR_CLUSTER_PIP_REQ=skip means workers have roar pre-installed — omit from pip."""
        monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", "skip")

        result = _merge_roar_runtime_env_pip(["numpy>=1.0"])

        # Either None or a list with no roar entry.
        if result is not None:
            roar_entries = [r for r in result if "roar" in r.lower()]
            assert not roar_entries, f"Expected no roar entry with skip, got: {roar_entries}"

    def test_no_duplicates_with_other_packages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other packages must be preserved and not duplicated."""
        monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", FAKE_WHEEL_URL)

        existing_pip = ["numpy>=1.0", "pandas==2.0", FAKE_WHEEL_URL]
        result = _merge_roar_runtime_env_pip(existing_pip)

        assert result is not None
        # No duplicates at all.
        seen: set[str] = set()
        for entry in result:
            assert entry not in seen, f"Duplicate entry in pip: {entry!r}\nFull list: {result}"
            seen.add(entry)

        # numpy and pandas preserved.
        assert any("numpy" in r for r in result), f"numpy missing from {result}"
        assert any("pandas" in r for r in result), f"pandas missing from {result}"
