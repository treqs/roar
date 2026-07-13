"""Unit tests for TagService.why — the provenance walk behind `roar tag why`.

`why` is a read-only traversal over the stored `{value, origin, job}` records,
the bind ledger, and job->inputs. These tests mock the db_ctx surface it reads
(labels.get_current, jobs.get_by_uid/get_inputs, artifacts.get) directly rather
than standing up the full project dependency chain, matching test_tag_service.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from roar.application.labels import LabelTargetRef
from roar.application.tags import TagService
from roar.core.label_origins import LABEL_ORIGIN_SYSTEM, LABEL_ORIGIN_USER


def _doc(tag_subtree: dict[str, Any]) -> dict[str, Any]:
    return {"metadata": {"tag": tag_subtree}, "version": 1}


def _val(value: str, origin: str, job: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"value": value, "origin": origin}
    if job is not None:
        record["job"] = job
    return record


def _make_service(
    *,
    labels: dict[str, dict[str, Any]],
    jobs_by_uid: dict[str, dict[str, Any]] | None = None,
    inputs_by_job_id: dict[int, list[dict[str, Any]]] | None = None,
    artifacts: dict[str, dict[str, Any]] | None = None,
) -> TagService:
    jobs_by_uid = jobs_by_uid or {}
    inputs_by_job_id = inputs_by_job_id or {}
    artifacts = artifacts or {}

    db_ctx = MagicMock()

    def get_current(
        entity_type: str,
        *,
        session_id: Any = None,
        job_id: Any = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any] | None:
        return labels.get(artifact_id) if artifact_id is not None else None

    db_ctx.labels.get_current.side_effect = get_current
    db_ctx.jobs.get_by_uid.side_effect = lambda uid: jobs_by_uid.get(uid)
    db_ctx.jobs.get_inputs.side_effect = lambda job_id: inputs_by_job_id.get(job_id, [])
    db_ctx.artifacts.get.side_effect = lambda aid: artifacts.get(aid)
    return TagService(db_ctx, Path("."))


def _artifact(artifact_id: str) -> LabelTargetRef:
    return LabelTargetRef(entity_type="artifact", artifact_id=artifact_id)


class TestWhy:
    def test_user_tag_add_is_a_leaf(self) -> None:
        svc = _make_service(
            labels={
                "raw": _doc({"contains_pii": {"values": [_val("present", LABEL_ORIGIN_USER)]}})
            },
            artifacts={"raw": {"path": "data/raw.csv"}},
        )
        roots = svc.why(_artifact("raw"), "contains_pii")
        assert len(roots) == 1
        assert "raw.csv" in roots[0].label
        assert "user `roar tag add`" in roots[0].label
        assert roots[0].children == []

    def test_system_value_walks_one_hop_to_the_user_act(self) -> None:
        svc = _make_service(
            labels={
                "out": _doc(
                    {"contains_pii": {"values": [_val("present", LABEL_ORIGIN_SYSTEM, "jobA")]}}
                ),
                "raw": _doc({"contains_pii": {"values": [_val("present", LABEL_ORIGIN_USER)]}}),
            },
            jobs_by_uid={"jobA": {"id": 1}},
            inputs_by_job_id={1: [{"artifact_id": "raw"}]},
            artifacts={"out": {"path": "out.txt"}, "raw": {"path": "raw.csv"}},
        )
        roots = svc.why(_artifact("out"), "contains_pii")
        assert len(roots) == 1
        assert "inherited" in roots[0].label and "jobA" in roots[0].label
        assert len(roots[0].children) == 1
        assert "user `roar tag add`" in roots[0].children[0].label

    def test_cross_session_hop_is_annotated_with_the_bind(self) -> None:
        svc = _make_service(
            labels={
                "out": _doc(
                    {"contains_pii": {"values": [_val("present", LABEL_ORIGIN_SYSTEM, "jobB")]}}
                ),
                "done": _doc(
                    {
                        "contains_pii": {"values": [_val("present", LABEL_ORIGIN_SYSTEM, "jobA")]},
                        "bind": {
                            "events": [{"action": "bind", "covers": {"contains_pii": ["present"]}}]
                        },
                    }
                ),
                "raw": _doc({"contains_pii": {"values": [_val("present", LABEL_ORIGIN_USER)]}}),
            },
            jobs_by_uid={"jobB": {"id": 2}, "jobA": {"id": 1}},
            inputs_by_job_id={2: [{"artifact_id": "done"}], 1: [{"artifact_id": "raw"}]},
            artifacts={
                "out": {"path": "out.txt"},
                "done": {"path": "done.marker"},
                "raw": {"path": "raw.csv"},
            },
        )
        roots = svc.why(_artifact("out"), "contains_pii")
        bind_node = roots[0].children[0]
        assert "roar tag bind" in bind_node.label and "done.marker" in bind_node.label
        # the bind wrapper still leads down to the originating human act
        derived = bind_node.children[0]
        assert "inherited" in derived.label
        assert "user `roar tag add`" in derived.children[0].label

    def test_add_tag_run_value_is_a_session_scoped_leaf(self) -> None:
        svc = _make_service(
            labels={"out": _doc({"license": {"values": [_val("MIT", LABEL_ORIGIN_USER, "jobA")]}})},
            artifacts={"out": {"path": "out.txt"}},
        )
        roots = svc.why(_artifact("out"), "license")
        assert "run --add-tag" in roots[0].label and "session-scoped" in roots[0].label
        assert roots[0].children == []

    def test_value_filter_narrows_to_one_value(self) -> None:
        svc = _make_service(
            labels={
                "raw": _doc(
                    {
                        "license": {
                            "values": [
                                _val("MIT", LABEL_ORIGIN_USER),
                                _val("GPL-3.0", LABEL_ORIGIN_USER),
                            ]
                        }
                    }
                )
            },
            artifacts={"raw": {"path": "raw.csv"}},
        )
        assert len(svc.why(_artifact("raw"), "license")) == 2
        narrowed = svc.why(_artifact("raw"), "license", "MIT")
        assert len(narrowed) == 1
        assert "MIT" in narrowed[0].label

    def test_absent_tag_returns_empty(self) -> None:
        svc = _make_service(labels={"out": _doc({})}, artifacts={"out": {"path": "out.txt"}})
        assert svc.why(_artifact("out"), "contains_pii") == []

    def test_missing_producer_job_is_a_graceful_leaf(self) -> None:
        svc = _make_service(
            labels={
                "out": _doc(
                    {"contains_pii": {"values": [_val("present", LABEL_ORIGIN_SYSTEM, "ghost")]}}
                )
            },
            jobs_by_uid={},  # producing job not found
            artifacts={"out": {"path": "out.txt"}},
        )
        roots = svc.why(_artifact("out"), "contains_pii")
        assert "producing job not found" in roots[0].label

    def test_cycle_is_guarded(self) -> None:
        # out is (contrived) produced by a job that lists out itself as an input.
        svc = _make_service(
            labels={
                "out": _doc(
                    {"contains_pii": {"values": [_val("present", LABEL_ORIGIN_SYSTEM, "jobA")]}}
                )
            },
            jobs_by_uid={"jobA": {"id": 1}},
            inputs_by_job_id={1: [{"artifact_id": "out"}]},
            artifacts={"out": {"path": "out.txt"}},
        )
        roots = svc.why(_artifact("out"), "contains_pii")
        # terminates (no infinite recursion); the self-edge is marked a cycle
        assert "(cycle)" in roots[0].children[0].label

    def test_why_rejects_job_target_with_actionable_message(self) -> None:
        svc = _make_service(labels={})
        # A job is a valid target elsewhere, so the error must name the job case
        # and point at output artifacts / `tag show` rather than implying the
        # reference was untracked.
        with pytest.raises(ValueError, match="not a job's"):
            svc.why(LabelTargetRef(entity_type="job", job_id=1), "contains_pii")

    def test_why_rejects_untracked_target(self) -> None:
        svc = _make_service(labels={})
        with pytest.raises(ValueError, match="tracked artifact"):
            svc.why(LabelTargetRef(entity_type="artifact", artifact_id=None), "contains_pii")
