from __future__ import annotations

from unittest.mock import MagicMock

from roar.application.publish.secrets import detect_lineage_secrets, filter_lineage_secrets
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext


def test_detect_lineage_secrets_scans_git_command_and_string_metadata() -> None:
    omit_filter = MagicMock()
    omit_filter.detect_secrets.side_effect = [
        ["git-secret"],
        ["command-secret"],
        ["metadata-secret"],
    ]
    omit_filter.get_detection_summary.return_value = ["git-secret", "command-secret"]

    result = detect_lineage_secrets(
        lineage=LineageData(
            jobs=[
                {
                    "command": "SECRET=1 python train.py",
                    "metadata": "token=abc",
                }
            ],
            artifacts=[],
            artifact_hashes=set(),
            pipeline=None,
        ),
        git_context=GitContext(
            repo="https://token@example.com/repo.git", branch="main", commit="deadbeef"
        ),
        omit_filter=omit_filter,
    )

    assert result == ["git-secret", "command-secret"]


def test_filter_lineage_secrets_redacts_command_and_metadata() -> None:
    omit_filter = MagicMock()
    omit_filter.filter_command.return_value = ("python train.py", ["cmd"])
    omit_filter.filter_telemetry.return_value = ("{}", ["telemetry"])
    omit_filter.filter_metadata.return_value = ({"safe": True}, ["metadata"])

    result = filter_lineage_secrets(
        lineage=LineageData(
            jobs=[
                {
                    "command": "SECRET=1 python train.py",
                    "metadata": "token=abc",
                },
                {
                    "command": "python eval.py",
                    "metadata": {"token": "abc"},
                },
            ],
            artifacts=[],
            artifact_hashes=set(),
            pipeline=None,
        ),
        omit_filter=omit_filter,
    )

    assert result.jobs[0]["command"] == "python train.py"
    assert result.jobs[0]["metadata"] == "{}"
    assert result.jobs[1]["metadata"] == {"safe": True}


def test_detect_lineage_secrets_flags_bare_userinfo_git_url_with_real_filter() -> None:
    from roar.filters.omit import OmitFilter

    detected = detect_lineage_secrets(
        lineage=LineageData(jobs=[], artifacts=[], artifact_hashes=set(), pipeline=None),
        git_context=GitContext(
            repo="https://" + "glpat-" + "Zx9kQ2mP4vL8nR3tW7yB" + "@gitlab.com/org/repo.git",
            branch="main",
            commit="deadbeef",
        ),
        omit_filter=OmitFilter({}),
    )

    assert "gitlab_token" in detected


def test_filter_git_context_secrets_redacts_embedded_credentials() -> None:
    from roar.application.publish.secrets import filter_git_context_secrets
    from roar.filters.omit import OmitFilter

    filtered, detections = filter_git_context_secrets(
        git_context=GitContext(
            repo="https://a94a8fe5ccb19ba61c4c0873d391e987982fbbd3@git.internal.co/org/repo.git",
            branch="main",
            commit="deadbeef",
        ),
        omit_filter=OmitFilter({}),
    )

    assert filtered.repo == "https://[REDACTED]@git.internal.co/org/repo.git"
    assert filtered.commit == "deadbeef"
    assert filtered.branch == "main"
    assert "git_url_userinfo" in detections


def test_filter_git_context_secrets_without_filter_returns_context_unchanged() -> None:
    from roar.application.publish.secrets import filter_git_context_secrets

    context = GitContext(
        repo="https://user:token123456@github.com/org/repo.git",
        branch="main",
        commit="deadbeef",
    )

    filtered, detections = filter_git_context_secrets(git_context=context, omit_filter=None)

    assert filtered is context
    assert detections == []


def test_hf_token_env_vars_are_redacted_by_default() -> None:
    """P0-16: HF_TOKEN / HUGGING_FACE_HUB_TOKEN must be in the built-in env-var
    redaction defaults so a live token doesn't reach a published DAG."""
    from roar.filters.omit import OmitFilter
    from roar.integrations.config.raw import _DEFAULT_REGISTRATION_OMIT

    names = OmitFilter(_DEFAULT_REGISTRATION_OMIT).env_var_names
    assert "HF_TOKEN" in names
    assert "HUGGING_FACE_HUB_TOKEN" in names


def test_hf_token_value_regex_catches_length_variants() -> None:
    """P0-16: the always-on HF value pattern is hf_[A-Za-z0-9]{20,}, not exact-34,
    so a token that isn't 34 chars is still caught (defense-in-depth)."""
    from roar.filters.omit import OmitFilter

    f = OmitFilter({})  # BUILTIN_PATTERNS apply regardless of config
    ids_30 = {m.pattern_id for m in f.detect_secrets("token=hf_" + "A" * 30)}
    ids_34 = {m.pattern_id for m in f.detect_secrets("token=hf_" + "A" * 34)}
    assert "huggingface_token" in ids_30  # 30 chars: the old {34} regex MISSED this
    assert "huggingface_token" in ids_34
