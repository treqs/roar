"""Tests for git-context handling in GLaaS registration validation.

Git context is optional: a run captured outside a git repository has no
repo/commit/branch, and that lineage is still registerable (GLaaS records it
with ``pin_mode: missing``). Only genuinely-required fields (session_hash,
command, job_uid, ...) are enforced.
"""

from __future__ import annotations

from roar.core.validation import (
    validate_job_registration,
    validate_session_registration,
)


def _valid_session_kwargs() -> dict:
    return {
        "session_hash": "a" * 64,
        "git_repo": "git@github.com:acme/widgets.git",
        "git_commit": "deadbeef",
        "git_branch": "main",
    }


def _valid_job_kwargs() -> dict:
    return {
        "command": "python train.py",
        "timestamp": 1_700_000_000.0,
        "session_hash": "a" * 64,
        "job_uid": "job123",
        "git_commit": "deadbeef",
        "git_branch": "main",
        "job_type": None,
        "step_number": 1,
    }


def test_session_valid_when_git_context_present() -> None:
    assert validate_session_registration(**_valid_session_kwargs())


def test_session_valid_without_git_context() -> None:
    # A run captured outside a git repo: no repo/commit/branch. Now allowed.
    kwargs = _valid_session_kwargs()
    kwargs.update(git_repo="", git_commit="", git_branch="")
    assert validate_session_registration(**kwargs)


def test_session_valid_with_partial_git_context() -> None:
    # Partial context (e.g. detached HEAD) is also accepted now.
    kwargs = _valid_session_kwargs()
    kwargs.update(git_branch="")
    assert validate_session_registration(**kwargs)


def test_session_hash_still_required_even_without_git() -> None:
    kwargs = _valid_session_kwargs()
    kwargs.update(session_hash="", git_repo="", git_commit="", git_branch="")
    result = validate_session_registration(**kwargs)
    assert not result
    assert result.errors == ["session_hash is required"]


def test_job_valid_when_git_context_present() -> None:
    assert validate_job_registration(**_valid_job_kwargs())


def test_job_valid_without_git_context() -> None:
    kwargs = _valid_job_kwargs()
    kwargs.update(git_commit="", git_branch="")
    assert validate_job_registration(**kwargs)


def test_job_required_fields_still_enforced_without_git() -> None:
    kwargs = _valid_job_kwargs()
    kwargs.update(command="", job_uid="", git_commit="", git_branch="")
    result = validate_job_registration(**kwargs)
    assert not result
    assert "command is required" in result.errors
    assert "job_uid is required" in result.errors
    # No git-related errors remain.
    assert not any("git" in e.lower() for e in result.errors)
