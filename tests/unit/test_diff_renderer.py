"""Unit tests for the diff renderer (presenters/diff_renderer.py)."""

from __future__ import annotations

import json

from roar.application.query.diff_graph import JobMatch, JobNode
from roar.application.query.results import AtomicDiff, ChangeType, DiffCategory, DiffResult
from roar.presenters.diff_renderer import DiffRenderer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(
    *,
    job_id: int = 1,
    command: str = "python train.py",
    step_number: int | None = 1,
) -> JobNode:
    return JobNode(
        job_id=job_id,
        job_uid="abc12345",
        command=command,
        step_number=step_number,
        git_commit="deadbeef",
        git_branch="main",
        metadata=None,
        input_hashes={},
        output_hashes={},
        input_paths={},
        output_paths={},
    )


def _result(
    *,
    diffs: list[AtomicDiff] | None = None,
    targets_identical: bool = False,
    matched_jobs: list[JobMatch] | None = None,
    only_in_a: list[JobNode] | None = None,
    only_in_b: list[JobNode] | None = None,
    root_cause: AtomicDiff | None = None,
    root_inputs_match: bool = True,
    root_inputs_a: dict[str, str] | None = None,
    root_inputs_b: dict[str, str] | None = None,
) -> DiffResult:
    return DiffResult(
        ref_a="./model_a.pt",
        ref_b="./model_b.pt",
        target_a_hash="aaa111",
        target_b_hash="bbb222",
        target_a_path="/out/model_a.pt",
        target_b_path="/out/model_b.pt",
        targets_identical=targets_identical,
        diffs=diffs or [],
        matched_jobs=matched_jobs or [],
        only_in_a=only_in_a or [],
        only_in_b=only_in_b or [],
        root_cause=root_cause,
        root_inputs_match=root_inputs_match,
        root_inputs_a=root_inputs_a or {},
        root_inputs_b=root_inputs_b or {},
    )


# ---------------------------------------------------------------------------
# Summary view
# ---------------------------------------------------------------------------


class TestSummaryView:
    def test_identical_targets(self):
        r = _result(targets_identical=True)
        output = DiffRenderer().render_text(r)
        assert "identical" in output.lower()

    def test_no_diffs(self):
        r = _result()
        output = DiffRenderer().render_text(r)
        assert "No differences found" in output

    def test_data_diff_shown(self):
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="Step @1: input 'data.csv' has different content",
            impact=0.9,
            is_root_cause=True,
        )
        r = _result(diffs=[diff], root_cause=diff)
        output = DiffRenderer().render_text(r)
        assert "DATA" in output
        assert "data.csv" in output
        assert "ROOT CAUSE" in output
        assert "[0.90]" in output

    def test_unchanged_section(self):
        ja = _job(job_id=1)
        jb = _job(job_id=2)
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="data changed",
            impact=0.5,
        )
        r = _result(diffs=[diff], matched_jobs=[JobMatch(ja, jb)])
        output = DiffRenderer().render_text(r)
        assert "UNCHANGED" in output
        assert "code (same git commit)" in output
        assert "environment (same packages)" in output
        assert "parameters (same arguments)" in output

    def test_structure_section_not_duplicated(self):
        """When PIPELINE diffs exist, STRUCTURE section should not appear."""
        j = _job(job_id=1, command="python extra.py", step_number=3)
        diff = AtomicDiff(
            change_type=ChangeType.ADDED,
            category=DiffCategory.PIPELINE,
            description="Step @3 only in B: python extra.py",
            detail={"step": "@3"},
            impact=0.5,
        )
        r = _result(diffs=[diff], only_in_b=[j])
        output = DiffRenderer().render_text(r)
        assert "PIPELINE" in output
        assert "STRUCTURE" not in output

    def test_inputs_same_artifacts_in_unchanged(self):
        ja = _job(job_id=1)
        jb = _job(job_id=2)
        diff = AtomicDiff(
            change_type=ChangeType.PARAM_CHANGED,
            category=DiffCategory.PARAMS,
            description="Step @1: --lr   A: 0.01   B: 0.001",
            impact=0.5,
        )
        r = _result(
            diffs=[diff],
            matched_jobs=[JobMatch(ja, jb)],
            root_inputs_match=True,
            root_inputs_a={"raw_d": "/data/raw.parquet"},
            root_inputs_b={"raw_d": "/data/raw.parquet"},
        )
        output = DiffRenderer().render_text(r)
        assert "inputs (same artifacts)" in output

    def test_inputs_section_when_root_inputs_differ(self):
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="Step @1: input differs",
            impact=0.9,
        )
        r = _result(
            diffs=[diff],
            root_inputs_match=False,
            root_inputs_a={"train_d": "/data/train.parquet"},
            root_inputs_b={"test_d": "/data/test.parquet"},
        )
        output = DiffRenderer().render_text(r)
        assert "INPUTS (root artifacts differ)" in output
        assert "A only: train.parquet" in output
        assert "B only: test.parquet" in output

    def test_data_category_suppressed_when_root_inputs_differ(self):
        """The INPUTS section supersedes the per-step DATA category."""
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="Step @1: input 'x' has different content",
            impact=0.9,
        )
        r = _result(
            diffs=[diff],
            root_inputs_match=False,
            root_inputs_a={"a_d": "/data/a"},
            root_inputs_b={"b_d": "/data/b"},
        )
        output = DiffRenderer().render_text(r)
        assert "INPUTS (root artifacts differ)" in output
        assert "DATA" not in output

    def test_data_category_shown_when_root_inputs_match(self):
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="Step @1: intermediate differs",
            impact=0.9,
        )
        r = _result(diffs=[diff], root_inputs_match=True)
        output = DiffRenderer().render_text(r)
        assert "DATA" in output


# ---------------------------------------------------------------------------
# Category view
# ---------------------------------------------------------------------------


class TestCategoryView:
    def test_groups_by_category(self):
        diffs = [
            AtomicDiff(
                change_type=ChangeType.CONTENT_CHANGED,
                category=DiffCategory.DATA,
                description="data diff",
                impact=0.9,
                is_root_cause=True,
            ),
            AtomicDiff(
                change_type=ChangeType.PARAM_CHANGED,
                category=DiffCategory.PARAMS,
                description="param diff",
                impact=0.7,
            ),
        ]
        r = _result(diffs=diffs)
        output = DiffRenderer().render_text(r, format="category")
        assert "DATA" in output
        assert "PARAMS" in output
        assert "[0.90]" in output
        assert "<-- root cause" in output

    def test_identical_targets(self):
        r = _result(targets_identical=True)
        output = DiffRenderer().render_text(r, format="category")
        assert "identical" in output.lower()


# ---------------------------------------------------------------------------
# DAG view
# ---------------------------------------------------------------------------


class TestDagView:
    def test_matched_identical_step(self):
        ja = _job(job_id=1, command="python preprocess.py", step_number=1)
        jb = _job(job_id=2, command="python preprocess.py", step_number=1)
        r = _result(matched_jobs=[JobMatch(ja, jb)])
        output = DiffRenderer().render_text(r, format="dag")
        assert "preprocess.py" in output
        assert "=" in output  # identical marker

    def test_matched_divergent_step(self):
        ja = _job(job_id=1, command="python train.py --lr 0.01", step_number=2)
        jb = _job(job_id=2, command="python train.py --lr 0.001", step_number=2)
        diff = AtomicDiff(
            change_type=ChangeType.PARAM_CHANGED,
            category=DiffCategory.PARAMS,
            description="lr changed",
            detail={"step": "@2"},
            impact=0.8,
            is_root_cause=True,
        )
        r = _result(diffs=[diff], matched_jobs=[JobMatch(ja, jb)])
        output = DiffRenderer().render_text(r, format="dag")
        assert "DIFFERS" in output
        assert "params" in output
        assert "ROOT CAUSE" in output

    def test_added_step(self):
        j = _job(job_id=3, command="python extra.py", step_number=3)
        r = _result(only_in_b=[j])
        output = DiffRenderer().render_text(r, format="dag")
        assert "ADDED" in output

    def test_removed_step(self):
        j = _job(job_id=3, command="python old.py", step_number=3)
        r = _result(only_in_a=[j])
        output = DiffRenderer().render_text(r, format="dag")
        assert "REMOVED" in output

    def test_python_module_step_uses_module_name(self):
        ja = _job(job_id=1, command="python -m pkg.train --lr 0.01", step_number=1)
        jb = _job(job_id=2, command="python -m pkg.train --lr 0.001", step_number=1)
        diff = AtomicDiff(
            change_type=ChangeType.PARAM_CHANGED,
            category=DiffCategory.PARAMS,
            description="lr changed",
            detail={"step": "@1"},
            impact=0.8,
        )

        r = _result(diffs=[diff], matched_jobs=[JobMatch(ja, jb)])
        output = DiffRenderer().render_text(r, format="dag")

        assert "pkg.train" in output
        assert "@1 -m" not in output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_basic_structure(self):
        diff = AtomicDiff(
            change_type=ChangeType.CONTENT_CHANGED,
            category=DiffCategory.DATA,
            description="data changed",
            detail={"step": "@1"},
            impact=0.9,
            is_root_cause=True,
        )
        r = _result(diffs=[diff], root_cause=diff)
        raw = DiffRenderer().render_json(r)
        data = json.loads(raw)

        assert data["ref_a"] == "./model_a.pt"
        assert data["ref_b"] == "./model_b.pt"
        assert data["targets_identical"] is False
        assert len(data["diffs"]) == 1
        assert data["diffs"][0]["impact"] == 0.9
        assert data["diffs"][0]["is_root_cause"] is True

    def test_root_cause_field(self):
        diff = AtomicDiff(
            change_type=ChangeType.PARAM_CHANGED,
            category=DiffCategory.PARAMS,
            description="lr changed",
            impact=0.85,
            is_root_cause=True,
        )
        r = _result(diffs=[diff], root_cause=diff)
        data = json.loads(DiffRenderer().render_json(r))
        assert data["root_cause"] is not None
        assert data["root_cause"]["category"] == "params"
        assert data["root_cause"]["impact"] == 0.85

    def test_no_root_cause(self):
        r = _result()
        data = json.loads(DiffRenderer().render_json(r))
        assert data["root_cause"] is None

    def test_root_inputs_fields_present(self):
        r = _result(
            root_inputs_match=False,
            root_inputs_a={"a_d": "/data/a.parquet"},
            root_inputs_b={"b_d": "/data/b.parquet"},
        )
        data = json.loads(DiffRenderer().render_json(r))
        assert data["root_inputs_match"] is False
        assert data["root_inputs_a"] == {"a_d": "/data/a.parquet"}
        assert data["root_inputs_b"] == {"b_d": "/data/b.parquet"}
