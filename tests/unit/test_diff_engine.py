"""Unit tests for the diff engine (application/query/diff.py)."""

from __future__ import annotations

from roar.application.query.diff_engine import (
    diff_args,
    diff_matched_jobs,
    extract_packages,
    extract_script_and_args,
    match_jobs,
    parse_kv_args,
    score_diffs,
)
from roar.application.query.diff_graph import JobMatch, JobNode, LineageGraph
from roar.application.query.diff_refs import classify_diff_ref
from roar.application.query.results import AtomicDiff, ChangeType, DiffCategory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(
    *,
    job_id: int = 1,
    job_uid: str = "abc12345",
    command: str = "python train.py --lr 0.01",
    step_number: int | None = 1,
    git_commit: str | None = "deadbeef",
    git_branch: str | None = "main",
    metadata: dict | None = None,
    input_hashes: dict | None = None,
    output_hashes: dict | None = None,
    input_paths: dict | None = None,
    output_paths: dict | None = None,
) -> JobNode:
    return JobNode(
        job_id=job_id,
        job_uid=job_uid,
        command=command,
        step_number=step_number,
        git_commit=git_commit,
        git_branch=git_branch,
        metadata=metadata,
        input_hashes=input_hashes or {},
        output_hashes=output_hashes or {},
        input_paths=input_paths or {},
        output_paths=output_paths or {},
    )


def _graph(jobs: list[JobNode], target_hash: str = "aaa") -> LineageGraph:
    return LineageGraph(
        target_artifact_id="art-1",
        target_hash=target_hash,
        target_path="/out/model.pt",
        jobs=jobs,
    )


# ---------------------------------------------------------------------------
# classify_diff_ref
# ---------------------------------------------------------------------------


class TestClassifyRef:
    def test_step_ref(self):
        assert classify_diff_ref("@5") == "job_step"
        assert classify_diff_ref("@B2") == "job_step"

    def test_file_path(self):
        assert classify_diff_ref("./model.pkl") == "file_path"
        assert classify_diff_ref("../data/out.csv") == "file_path"
        assert classify_diff_ref("~/models/x.pt") == "file_path"
        assert classify_diff_ref("path/to/file") == "file_path"

    def test_job_uid(self):
        assert classify_diff_ref("deadbeef") == "job_uid"
        assert classify_diff_ref("abc1") == "job_uid"

    def test_artifact_hash(self):
        assert classify_diff_ref("deadbeef01234567") == "artifact_hash"
        assert classify_diff_ref("a" * 64) == "artifact_hash"

    def test_glaas_prefix(self):
        assert classify_diff_ref("glaas:abc123") == "glaas"

    def test_session_prefix(self):
        assert classify_diff_ref("session:current") == "session"
        assert classify_diff_ref("session:abc123") == "session"

    def test_path_candidate(self):
        assert classify_diff_ref("model.pkl") == "path_candidate"
        assert classify_diff_ref("some-name") == "path_candidate"


# ---------------------------------------------------------------------------
# extract_script_and_args
# ---------------------------------------------------------------------------


class TestExtractScriptAndArgs:
    def test_python_script(self):
        script, args = extract_script_and_args("python train.py --lr 0.01 --epochs 10")
        assert script == "train.py"
        assert args == ["--lr", "0.01", "--epochs", "10"]

    def test_python3_script(self):
        script, args = extract_script_and_args("python3 evaluate.py --model m.pkl")
        assert script == "evaluate.py"
        assert args == ["--model", "m.pkl"]

    def test_bare_command(self):
        script, _args = extract_script_and_args("wget http://example.com -O out.txt")
        assert script == "wget"

    def test_nested_path(self):
        script, args = extract_script_and_args("python /path/to/train.py --lr 0.01")
        assert script == "train.py"
        assert args == ["--lr", "0.01"]

    def test_python_module(self):
        script, args = extract_script_and_args("python -m pkg.train --lr 0.01 --epochs 10")
        assert script == "pkg.train"
        assert args == ["--lr", "0.01", "--epochs", "10"]


# ---------------------------------------------------------------------------
# parse_kv_args
# ---------------------------------------------------------------------------


class TestParseKvArgs:
    def test_double_dash_with_value(self):
        result = parse_kv_args(["--lr", "0.01", "--epochs", "10"])
        assert result == {"--lr": "0.01", "--epochs": "10"}

    def test_equals_syntax(self):
        result = parse_kv_args(["--lr=0.01", "--epochs=10"])
        assert result == {"--lr": "0.01", "--epochs": "10"}

    def test_flag_only(self):
        result = parse_kv_args(["--verbose", "--lr", "0.01"])
        assert result["--verbose"] == ""
        assert result["--lr"] == "0.01"

    def test_positional_args_ignored(self):
        result = parse_kv_args(["input.csv", "output.csv"])
        assert result == {}

    def test_mixed(self):
        result = parse_kv_args(["--input", "data.csv", "positional", "--output", "model.pt"])
        assert result == {"--input": "data.csv", "--output": "model.pt"}


# ---------------------------------------------------------------------------
# diff_args
# ---------------------------------------------------------------------------


class TestDiffArgs:
    def test_param_changed(self):
        diffs = diff_args(
            "python train.py --lr 0.01 --epochs 10",
            "python train.py --lr 0.001 --epochs 10",
        )
        assert len(diffs) == 1
        assert diffs[0]["param"] == "--lr"
        assert diffs[0]["old"] == "0.01"
        assert diffs[0]["new"] == "0.001"

    def test_param_added(self):
        diffs = diff_args(
            "python train.py --lr 0.01",
            "python train.py --lr 0.01 --epochs 10",
        )
        assert len(diffs) == 1
        assert diffs[0]["param"] == "--epochs"
        assert diffs[0]["old"] == "(absent)"
        assert diffs[0]["new"] == "10"

    def test_identical_args(self):
        diffs = diff_args(
            "python train.py --lr 0.01",
            "python train.py --lr 0.01",
        )
        assert diffs == []

    def test_no_structured_args(self):
        diffs = diff_args("wget http://a.com", "wget http://b.com")
        assert diffs == []


# ---------------------------------------------------------------------------
# extract_packages
# ---------------------------------------------------------------------------


class TestExtractPackages:
    def test_dict_format(self):
        meta = {"packages": {"pip": {"numpy": "1.24.0", "scipy": "1.11.0"}}}
        result = extract_packages(meta)
        assert result == {"numpy": "1.24.0", "scipy": "1.11.0"}

    def test_list_format_with_equals(self):
        meta = {"pip_packages": ["numpy==1.24.0", "scipy==1.11.0"]}
        result = extract_packages(meta)
        assert result == {"numpy": "1.24.0", "scipy": "1.11.0"}

    def test_list_format_with_dicts(self):
        meta = {"pip_packages": [{"name": "numpy", "version": "1.24.0"}]}
        result = extract_packages(meta)
        assert result == {"numpy": "1.24.0"}

    def test_empty(self):
        assert extract_packages({}) == {}
        assert extract_packages({"packages": {}}) == {}


# ---------------------------------------------------------------------------
# match_jobs
# ---------------------------------------------------------------------------


class TestMatchJobs:
    def test_exact_command_match(self):
        ja = _job(job_id=1, command="python train.py --lr 0.01")
        jb = _job(job_id=2, command="python train.py --lr 0.01")
        ga = _graph([ja])
        gb = _graph([jb])

        matched, only_a, only_b = match_jobs(ga, gb)
        assert len(matched) == 1
        assert matched[0].job_a.job_id == 1
        assert matched[0].job_b.job_id == 2
        assert only_a == []
        assert only_b == []

    def test_script_name_match(self):
        ja = _job(job_id=1, command="python train.py --lr 0.01")
        jb = _job(job_id=2, command="python train.py --lr 0.001")
        ga = _graph([ja])
        gb = _graph([jb])

        matched, only_a, only_b = match_jobs(ga, gb)
        assert len(matched) == 1
        assert only_a == []
        assert only_b == []

    def test_no_match(self):
        ja = _job(job_id=1, command="python train.py")
        jb = _job(job_id=2, command="python evaluate.py")
        ga = _graph([ja])
        gb = _graph([jb])

        matched, only_a, only_b = match_jobs(ga, gb)
        assert matched == []
        assert len(only_a) == 1
        assert len(only_b) == 1

    def test_multiple_jobs(self):
        ga = _graph(
            [
                _job(job_id=1, command="python preprocess.py", step_number=1),
                _job(job_id=2, command="python train.py --lr 0.01", step_number=2),
            ]
        )
        gb = _graph(
            [
                _job(job_id=3, command="python preprocess.py", step_number=1),
                _job(job_id=4, command="python train.py --lr 0.001", step_number=2),
            ]
        )

        matched, only_a, only_b = match_jobs(ga, gb)
        assert len(matched) == 2
        assert only_a == []
        assert only_b == []

    def test_added_step(self):
        ga = _graph([_job(job_id=1, command="python train.py")])
        gb = _graph(
            [
                _job(job_id=2, command="python preprocess.py", step_number=1),
                _job(job_id=3, command="python train.py", step_number=2),
            ]
        )

        matched, only_a, only_b = match_jobs(ga, gb)
        assert len(matched) == 1
        assert only_a == []
        assert len(only_b) == 1
        assert only_b[0].command == "python preprocess.py"

    def test_python_module_names_do_not_false_match(self):
        ja = _job(job_id=1, command="python -m pkg.train --lr 0.01")
        jb = _job(job_id=2, command="python -m pkg.evaluate --lr 0.01")

        matched, only_a, only_b = match_jobs(_graph([ja]), _graph([jb]))

        assert matched == []
        assert only_a == [ja]
        assert only_b == [jb]


# ---------------------------------------------------------------------------
# diff_matched_jobs
# ---------------------------------------------------------------------------


class TestDiffMatchedJobs:
    def test_identical_jobs_no_diffs(self):
        j = _job(input_hashes={"a": "hash1"}, input_paths={"a": "/data/in.csv"})
        diffs = diff_matched_jobs([JobMatch(j, j)])
        assert diffs == []

    def test_param_diff(self):
        ja = _job(command="python train.py --lr 0.01")
        jb = _job(command="python train.py --lr 0.001")
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        param_diffs = [d for d in diffs if d.category == DiffCategory.PARAMS]
        assert len(param_diffs) == 1
        assert "--lr" in param_diffs[0].description

    def test_code_diff(self):
        ja = _job(git_commit="aaaa1111")
        jb = _job(git_commit="bbbb2222")
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        code_diffs = [d for d in diffs if d.category == DiffCategory.CODE]
        assert len(code_diffs) == 1
        assert "aaaa1111" in code_diffs[0].description

    def test_input_content_changed(self):
        ja = _job(
            input_hashes={"a": "hash_old"},
            input_paths={"a": "/data/features.npz"},
        )
        jb = _job(
            input_hashes={"b": "hash_new"},
            input_paths={"b": "/data/features.npz"},
        )
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        data_diffs = [d for d in diffs if d.category == DiffCategory.DATA]
        assert len(data_diffs) == 1
        assert data_diffs[0].change_type == ChangeType.CONTENT_CHANGED

    def test_different_input_names_paired_positionally(self):
        ja = _job(
            input_hashes={"a": "hash1"},
            input_paths={"a": "/data/test_feats.npz"},
        )
        jb = _job(
            input_hashes={"b": "hash2"},
            input_paths={"b": "/data/train_feats.npz"},
        )
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        data_diffs = [d for d in diffs if d.category == DiffCategory.DATA]
        assert len(data_diffs) == 1
        assert "test_feats.npz" in data_diffs[0].description
        assert "train_feats.npz" in data_diffs[0].description
        # Should NOT have added/removed, only the positional comparison
        assert all(d.change_type != ChangeType.ADDED for d in data_diffs)
        assert all(d.change_type != ChangeType.REMOVED for d in data_diffs)

    def test_same_hash_renamed_inputs_do_not_register_as_content_change(self):
        ja = _job(
            input_hashes={"a": "same-hash"},
            input_paths={"a": "/data/train_feats.npz"},
        )
        jb = _job(
            input_hashes={"b": "same-hash"},
            input_paths={"b": "/data/eval_feats.npz"},
        )

        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        data_diffs = [d for d in diffs if d.category == DiffCategory.DATA]

        assert data_diffs == []

    def test_env_diff(self):
        ja = _job(metadata={"packages": {"pip": {"scipy": "1.16.1"}}})
        jb = _job(metadata={"packages": {"pip": {"scipy": "1.17.1"}}})
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        env_diffs = [d for d in diffs if d.category == DiffCategory.COMPUTE]
        assert len(env_diffs) == 1
        assert "scipy" in env_diffs[0].description

    def test_env_diff_package_added(self):
        ja = _job(metadata={"packages": {"pip": {"numpy": "1.24.0"}}})
        jb = _job(metadata={"packages": {"pip": {"numpy": "1.24.0", "wandb": "0.15.0"}}})
        diffs = diff_matched_jobs([JobMatch(ja, jb)])
        env_diffs = [d for d in diffs if d.category == DiffCategory.COMPUTE]
        assert len(env_diffs) == 1
        assert "wandb" in env_diffs[0].description


# ---------------------------------------------------------------------------
# score_diffs
# ---------------------------------------------------------------------------


class TestScoreDiffs:
    def test_earlier_steps_score_higher(self):
        ga = _graph(
            [
                _job(job_id=1, step_number=1),
                _job(job_id=2, step_number=3),
            ]
        )
        gb = _graph(
            [
                _job(job_id=3, step_number=1),
                _job(job_id=4, step_number=3),
            ]
        )
        diffs = [
            AtomicDiff(
                change_type=ChangeType.PARAM_CHANGED,
                category=DiffCategory.PARAMS,
                description="step 1 change",
                detail={"step": "@1"},
            ),
            AtomicDiff(
                change_type=ChangeType.PARAM_CHANGED,
                category=DiffCategory.PARAMS,
                description="step 3 change",
                detail={"step": "@3"},
            ),
        ]
        scored = score_diffs(diffs, ga, gb)
        assert scored[0].impact > scored[1].impact
        assert scored[0].detail["step"] == "@1"

    def test_root_cause_marked(self):
        ga = _graph([_job(step_number=1)])
        gb = _graph([_job(step_number=1)])
        diffs = [
            AtomicDiff(
                change_type=ChangeType.CONTENT_CHANGED,
                category=DiffCategory.DATA,
                description="data changed",
                detail={"step": "@1"},
            ),
        ]
        scored = score_diffs(diffs, ga, gb)
        assert scored[0].is_root_cause is True

    def test_severity_ordering(self):
        """Content changes should score higher than env changes at the same step."""
        ga = _graph([_job(step_number=1)])
        gb = _graph([_job(step_number=1)])
        diffs = [
            AtomicDiff(
                change_type=ChangeType.ENV_CHANGED,
                category=DiffCategory.COMPUTE,
                description="env",
                detail={"step": "@1"},
            ),
            AtomicDiff(
                change_type=ChangeType.CONTENT_CHANGED,
                category=DiffCategory.DATA,
                description="data",
                detail={"step": "@1"},
            ),
        ]
        scored = score_diffs(diffs, ga, gb)
        assert scored[0].change_type == ChangeType.CONTENT_CHANGED
        assert scored[1].change_type == ChangeType.ENV_CHANGED

    def test_empty_diffs(self):
        ga = _graph([])
        gb = _graph([])
        assert score_diffs([], ga, gb) == []
