"""Lazy exports for local query and label workflows."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "DagQueryRequest": ".requests",
    "LabelCopyRequest": ".requests",
    "LabelHistoryRequest": ".requests",
    "LabelPushRequest": ".requests",
    "LabelSetRequest": ".requests",
    "LabelShowRequest": ".requests",
    "LineageQueryRequest": ".requests",
    "LogQueryRequest": ".requests",
    "ShowQueryRequest": ".requests",
    "StatusQueryRequest": ".requests",
    "LabelCurrentSummary": ".results",
    "LabelHistorySummary": ".results",
    "LineageSummary": ".results",
    "LogSummary": ".results",
    "ShowSummary": ".results",
    "StatusSummary": ".results",
    "build_copy_labels_summary": ".label",
    "build_label_history_summary": ".label",
    "build_push_labels_summary": ".label",
    "build_set_labels_summary": ".label",
    "build_show_labels_summary": ".label",
    "copy_labels": ".label",
    "label_history": ".label",
    "render_dag": ".dag",
    "render_lineage": ".lineage",
    "render_log": ".log",
    "render_show": ".show",
    "render_status": ".status",
    "push_labels": ".label",
    "set_labels": ".label",
    "show_labels": ".label",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
