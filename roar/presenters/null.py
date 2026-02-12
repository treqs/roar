"""Null presenter used when no interactive output sink is provided."""

from __future__ import annotations

from typing import Any

from ..core.interfaces.presenter import IPresenter


class NullPresenter(IPresenter):
    """No-op presenter that preserves service-level output contracts."""

    def print(self, message: str) -> None:
        return None

    def print_error(self, message: str) -> None:
        return None

    def print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        return None

    def print_job(self, job: dict[str, Any], verbose: bool = False) -> None:
        return None

    def print_artifact(self, artifact: dict[str, Any]) -> None:
        return None

    def print_dag(
        self,
        summary: dict[str, Any],
        stale_steps: set[int] | None = None,
    ) -> None:
        return None

    def confirm(self, message: str, default: bool = False) -> bool:
        return default
