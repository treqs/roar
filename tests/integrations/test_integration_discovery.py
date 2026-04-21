from __future__ import annotations

from types import SimpleNamespace

from roar.core.bootstrap import reset
from roar.core.interfaces.telemetry import ITelemetryProvider
from roar.core.interfaces.vcs import IVCSProvider
from roar.core.models.telemetry import TelemetryRunInfo
from roar.core.models.vcs import VCSInfo
from roar.integrations import (
    discover_optional_integrations,
    list_telemetry_providers,
    list_vcs_providers,
)


class _ExampleTelemetryProvider(ITelemetryProvider):
    @property
    def name(self) -> str:
        return "example"

    def detect_runs(
        self,
        repo_root: str,
        start_time: float,
        end_time: float,
        allow_incomplete: bool = False,
    ) -> list[TelemetryRunInfo]:
        return []

    def get_run_url(self, run_id: str) -> str | None:
        return None


class _ExampleVcsProvider(IVCSProvider):
    @property
    def name(self) -> str:
        return "example-vcs"

    def get_repo_root(self, path: str | None = None) -> str | None:
        return path

    def get_info(self, repo_root: str) -> VCSInfo:
        del repo_root
        return VCSInfo()

    def get_status(self, repo_root: str) -> tuple[bool, list[str]]:
        del repo_root
        return True, []

    def is_tracked(self, repo_root: str, path: str) -> bool:
        del repo_root, path
        return True

    def classify_file(self, repo_root: str, path: str) -> str:
        del repo_root, path
        return "tracked"


def test_discover_optional_integrations_registers_typed_entrypoint_telemetry(monkeypatch) -> None:
    reset()

    def fake_entry_points(*, group: str):
        if group == "roar.telemetry_providers":
            return [
                SimpleNamespace(
                    name="example",
                    load=lambda: _ExampleTelemetryProvider,
                )
            ]
        if group == "roar.vcs_providers":
            return []
        if group == "roar.integrations":
            return []
        raise AssertionError(f"Unexpected group: {group}")

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    discover_optional_integrations()

    assert "example" in list_telemetry_providers()


def test_discover_optional_integrations_registers_typed_entrypoint_vcs(monkeypatch) -> None:
    reset()

    def fake_entry_points(*, group: str):
        if group == "roar.telemetry_providers":
            return []
        if group == "roar.vcs_providers":
            return [
                SimpleNamespace(
                    name="example-vcs",
                    load=lambda: _ExampleVcsProvider,
                )
            ]
        if group == "roar.integrations":
            return []
        raise AssertionError(f"Unexpected group: {group}")

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    discover_optional_integrations()

    assert "example-vcs" in list_vcs_providers()


def test_discover_optional_integrations_keeps_legacy_group_compatibility(monkeypatch) -> None:
    reset()

    def fake_entry_points(*, group: str):
        if group in {"roar.telemetry_providers", "roar.vcs_providers"}:
            return []
        if group == "roar.integrations":
            return [
                SimpleNamespace(
                    name="example",
                    load=lambda: _ExampleTelemetryProvider,
                )
            ]
        raise AssertionError(f"Unexpected group: {group}")

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    discover_optional_integrations()

    assert "example" in list_telemetry_providers()
