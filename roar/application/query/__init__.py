"""Lazy exports for local query and label workflows."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "DagQueryRequest": ".requests",
    "LabelCopyRequest": ".requests",
    "LabelHistoryRequest": ".requests",
    "LabelSetRequest": ".requests",
    "LabelShowRequest": ".requests",
    "LabelSyncRequest": ".requests",
    "LabelUnsetRequest": ".requests",
    "RemoteLabelHistoryRequest": ".requests",
    "RemoteLabelSetRequest": ".requests",
    "RemoteLabelShowRequest": ".requests",
    "RemoteLabelUnsetRequest": ".requests",
    "LineageQueryRequest": ".requests",
    "LogQueryRequest": ".requests",
    "ShowQueryRequest": ".requests",
    "StatusQueryRequest": ".requests",
    "TagAddRequest": ".requests",
    "TagHistoryRequest": ".requests",
    "TagRmRequest": ".requests",
    "TagShowRequest": ".requests",
    "LabelCurrentSummary": ".results",
    "LabelHistorySummary": ".results",
    "LineageSummary": ".results",
    "LogSummary": ".results",
    "ShowSummary": ".results",
    "StatusSummary": ".results",
    "build_copy_labels_summary": ".label",
    "build_label_history_summary": ".label",
    "build_remote_label_history_summary": ".label",
    "build_remote_set_labels_summary": ".label",
    "build_remote_show_labels_summary": ".label",
    "build_remote_unset_labels_summary": ".label",
    "build_set_labels_summary": ".label",
    "build_show_labels_summary": ".label",
    "build_sync_labels_summary": ".label",
    "build_unset_labels_summary": ".label",
    "build_tag_add_summary": ".tag",
    "build_tag_history_summary": ".tag",
    "build_tag_rm_summary": ".tag",
    "build_tag_show_summary": ".tag",
    "copy_labels": ".label",
    "label_history": ".label",
    "remote_label_history": ".label",
    "remote_set_labels": ".label",
    "remote_show_labels": ".label",
    "remote_unset_labels": ".label",
    "render_dag": ".dag",
    "render_lineage": ".lineage",
    "render_log": ".log",
    "render_show": ".show",
    "render_status": ".status",
    "set_labels": ".label",
    "show_labels": ".label",
    "sync_labels": ".label",
    "tag_add": ".tag",
    "tag_history": ".tag",
    "tag_rm": ".tag",
    "tag_show": ".tag",
    "unset_labels": ".label",
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
