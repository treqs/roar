from __future__ import annotations

from types import SimpleNamespace

from roar.core.bootstrap import reset
from roar.core.interfaces.telemetry import ITelemetryProvider, TelemetryRunInfo
from roar.integrations import list_telemetry_providers
from roar.plugins import discover_plugins


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


def test_discover_plugins_registers_entrypoint_telemetry(monkeypatch) -> None:
    reset()

    def fake_entry_points(*, group: str):
        assert group == "roar.plugins"
        return [
            SimpleNamespace(
                name="example",
                load=lambda: _ExampleTelemetryProvider,
            )
        ]

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    discover_plugins()

    assert "example" in list_telemetry_providers()
