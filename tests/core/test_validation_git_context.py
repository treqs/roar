"""Tests for git-context validation in GLaaS registration payloads.

Covers the relaxed-repo behavior: a run captured outside a git repository
has no git context at all, and the validators should surface that as one
clear, actionable message rather than three cryptic per-field errors —
while still giving granular diagnostics for partial cases (detached HEAD,
empty repo) where only some fields are missing.
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


def test_session_no_git_context_collapses_to_single_message() -> None:
    kwargs = _valid_session_kwargs()
    kwargs.update(git_repo="", git_commit="", git_branch="")
    result = validate_session_registration(**kwargs)
    assert not result
    # One combined message, not three per-field lines.
    assert len(result.errors) == 1
    msg = result.errors[0]
    assert "not inside a git repository" in msg
    assert "register" in msg


def test_session_partial_git_context_keeps_granular_messages() -> None:
    # Repo + commit present, only branch missing (detached-HEAD-ish):
    # the granular per-field message must survive.
    kwargs = _valid_session_kwargs()
    kwargs.update(git_branch="")
    result = validate_session_registration(**kwargs)
    assert not result
    assert result.errors == ["git_branch is required (detached HEAD?)"]


def test_session_hash_error_is_independent_of_git_collapse() -> None:
    kwargs = _valid_session_kwargs()
    kwargs.update(session_hash="", git_repo="", git_commit="", git_branch="")
    result = validate_session_registration(**kwargs)
    assert not result
    assert "session_hash is required" in result.errors
    # Git context still collapses to its single combined message alongside it.
    assert any("not inside a git repository" in e for e in result.errors)


def test_job_valid_when_git_context_present() -> None:
    assert validate_job_registration(**_valid_job_kwargs())


def test_job_no_git_context_collapses_to_single_message() -> None:
    kwargs = _valid_job_kwargs()
    kwargs.update(git_commit="", git_branch="")
    result = validate_job_registration(**kwargs)
    assert not result
    git_errors = [e for e in result.errors if "git" in e.lower()]
    assert len(git_errors) == 1
    assert "not inside a git repository" in git_errors[0]


def test_job_partial_git_context_keeps_granular_message() -> None:
    kwargs = _valid_job_kwargs()
    kwargs.update(git_branch="")
    result = validate_job_registration(**kwargs)
    assert not result
    assert "git_branch is required" in result.errors
