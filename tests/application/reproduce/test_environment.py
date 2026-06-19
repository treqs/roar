"""Tests for the reproduction code-source resolver."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from roar.application.reproduce import environment as env
from roar.application.reproduce.environment import resolve_code_source
from roar.core.interfaces.reproduction import PipelineInfo


def _make_pipeline(**kwargs):
    defaults = {
        "artifact_hash": "abc123",
        "git_repo": "https://github.com/user/repo.git",
        "git_commit": "deadbeefcafe",
        "build_steps": [],
        "run_steps": [],
        "total_steps": 0,
    }
    defaults.update(kwargs)
    return PipelineInfo(**defaults)


def _ok(*_a, **_k):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch(**overrides):
    """Patch the resolver's git helpers; sensible defaults, override per test."""
    defaults = {
        "_git_toplevel": lambda cwd: None,
        "_git_origin": lambda root: None,
        "_commit_exists": lambda root, commit: False,
        "_uncommitted_modifications": lambda root: 0,
        "_git_fetch": lambda root: None,
        "_run_git": _ok,
    }
    defaults.update(overrides)
    return [patch.object(env, name, fn) for name, fn in defaults.items()]


def _run(cwd, pipeline=None, **overrides):
    patches = _patch(**overrides)
    for p in patches:
        p.start()
    try:
        return resolve_code_source(cwd, pipeline or _make_pipeline(), presenter=MagicMock())
    finally:
        for p in patches:
            p.stop()


def test_matching_repo_reuses_in_place(tmp_path):
    plan = _run(
        tmp_path,
        _git_toplevel=lambda cwd: str(tmp_path),
        _commit_exists=lambda root, commit: True,  # recorded commit is here
        _uncommitted_modifications=lambda root: 0,
    )
    assert plan.kind == "reuse"
    assert plan.repo_dir == tmp_path


def test_matching_repo_uncommitted_modifications_error(tmp_path):
    with pytest.raises(ValueError, match="uncommitted changes"):
        _run(
            tmp_path,
            _git_toplevel=lambda cwd: str(tmp_path),
            _commit_exists=lambda root, commit: True,
            _uncommitted_modifications=lambda root: 2,
        )


def test_matching_repo_deleted_output_is_reused_not_blocked(tmp_path):
    # Deleting an output to recreate it isn't "uncommitted work" -> reuse, no error.
    plan = _run(
        tmp_path,
        _git_toplevel=lambda cwd: str(tmp_path),
        _commit_exists=lambda root, commit: True,
        _uncommitted_modifications=lambda root: 0,  # deletions don't count
    )
    assert plan.kind == "reuse"


def test_inside_nonmatching_repo_errors(tmp_path):
    with pytest.raises(ValueError, match="doesn't match this lineage"):
        _run(
            tmp_path,
            _git_toplevel=lambda cwd: str(tmp_path),
            _git_origin=lambda root: "git@github.com:other/project.git",
            _commit_exists=lambda root, commit: False,
        )


def test_matching_by_origin_fetches_then_reuses(tmp_path):
    state = {"fetched": False}

    def fetch(root):
        state["fetched"] = True

    def commit_exists(root, commit):
        return state["fetched"]

    plan = _run(
        tmp_path,
        _git_toplevel=lambda cwd: str(tmp_path),
        _git_origin=lambda root: "git@github.com:user/repo.git",
        _commit_exists=commit_exists,
        _git_fetch=fetch,
    )
    assert state["fetched"] is True
    assert plan.kind == "reuse"


def test_not_in_repo_with_remote_clones(tmp_path):
    plan = _run(tmp_path, _git_toplevel=lambda cwd: None)
    assert plan.kind == "clone"
    assert plan.repo_dir is None


def test_no_repo_no_commit_reruns(tmp_path):
    plan = _run(
        tmp_path,
        _make_pipeline(git_repo="", git_commit=""),
        _git_toplevel=lambda cwd: None,
    )
    assert plan.kind == "rerun"
    assert plan.repo_dir == tmp_path / "reproduce" / "rerun"


def test_commit_recorded_no_remote_not_in_repo_errors(tmp_path):
    with pytest.raises(ValueError, match="no published remote"):
        _run(
            tmp_path,
            _make_pipeline(git_repo="", git_commit="deadbeefcafe"),
            _git_toplevel=lambda cwd: None,
        )


# --- prepare_reproduction_environment dispatch --------------------------------
# These pin which EnvironmentSetupService method each resolved plan routes to.
# autospec=True means the patch fails to even start if the method is removed from
# the real class, so a "clone" plan must hit setup() and a "rerun" plan must hit
# setup_in_place() -- guarding against the regression where the in-place builder
# was folded into setup() but the rerun caller was left dangling.


def _prepare(plan, pipeline, tmp_path):
    with (
        patch.object(env, "resolve_code_source", return_value=plan),
        patch.object(env, "EnvironmentSetupService", autospec=True) as mock_service_cls,
    ):
        service = mock_service_cls.return_value
        env.prepare_reproduction_environment(
            pipeline=pipeline,
            cwd=tmp_path,
            presenter=MagicMock(),
            auto_confirm=True,
        )
    return service


def test_clone_plan_dispatches_to_setup(tmp_path):
    plan = env.CodeSourcePlan(kind="clone", repo_dir=None)
    service = _prepare(plan, _make_pipeline(), tmp_path)
    service.setup.assert_called_once()
    service.setup_in_place.assert_not_called()


def test_rerun_plan_dispatches_to_setup_in_place(tmp_path):
    scratch = tmp_path / "reproduce" / "rerun"
    plan = env.CodeSourcePlan(kind="rerun", repo_dir=scratch)
    pipeline = _make_pipeline(git_repo="", git_commit="")
    service = _prepare(plan, pipeline, tmp_path)

    service.setup_in_place.assert_called_once()
    service.setup.assert_not_called()
    pos_args = service.setup_in_place.call_args.args
    assert pos_args[0] is pipeline
    assert pos_args[1] == scratch
