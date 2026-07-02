"""Real-regex coverage for git URLs with embedded secrets in OmitFilter.

Git remotes can embed credentials in several shapes beyond the classic
``https://user:token@host`` form (bare userinfo PATs, empty usernames,
ssh passwords, query-param tokens). These tests pin every shape against
the actual built-in patterns — no mocks — so a filter regression cannot
silently republish a credential.
"""

from __future__ import annotations

import pytest

from roar.filters.omit import OmitFilter

# Assembled at runtime so secret scanners do not flag the fixture as a real PAT.
FAKE_GITLAB_PAT = "glpat-" + "Zx9kQ2mP4vL8nR3tW7yB"


@pytest.fixture()
def omit_filter() -> OmitFilter:
    return OmitFilter({})


SECRET_URL_CASES = [
    pytest.param(
        "https://user:supersecrettoken123@github.com/org/repo.git",
        "supersecrettoken123",
        "https://user:[REDACTED]@github.com/org/repo.git",
        id="https-user-pass",
    ),
    pytest.param(
        f"https://{FAKE_GITLAB_PAT}@gitlab.com/org/repo.git",
        FAKE_GITLAB_PAT,
        "https://[GITLAB_TOKEN_REDACTED]@gitlab.com/org/repo.git",
        id="bare-gitlab-pat-userinfo",
    ),
    pytest.param(
        "https://a94a8fe5ccb19ba61c4c0873d391e987982fbbd3@git.internal.co/org/repo.git",
        "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
        "https://[REDACTED]@git.internal.co/org/repo.git",
        id="bare-oauth-token-userinfo",
    ),
    pytest.param(
        "https://ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@github.com/org/repo.git",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "https://[GITHUB_TOKEN_REDACTED]@github.com/org/repo.git",
        id="bare-github-pat-userinfo",
    ),
    pytest.param(
        "https://:supersecrettoken123@github.com/org/repo.git",
        "supersecrettoken123",
        "https://:[REDACTED]@github.com/org/repo.git",
        id="empty-user-token-password",
    ),
    pytest.param(
        "ssh://deploy:supersecretpass@git.internal.co/org/repo.git",
        "supersecretpass",
        "ssh://deploy:[REDACTED]@git.internal.co/org/repo.git",
        id="ssh-user-pass",
    ),
    pytest.param(
        "https://x-access-token:ghs_16C7e42F292c6912E7710c838347Ae178B4a@github.com/org/repo.git",
        "ghs_16C7e42F292c6912E7710c838347Ae178B4a",
        "https://x-access-token:[REDACTED]@github.com/org/repo.git",
        id="github-actions-installation-token",
    ),
    pytest.param(
        "git+https://user:tok123456@github.com/org/repo.git",
        "tok123456",
        "git+https://user:[REDACTED]@github.com/org/repo.git",
        id="pip-style-git-https",
    ),
    pytest.param(
        "https://deploytoken@git.internal.co/org/repo.git",
        "deploytoken@",
        "https://[REDACTED]@git.internal.co/org/repo.git",
        id="bare-userinfo-fails-closed",
    ),
]


@pytest.mark.parametrize(("url", "secret", "expected"), SECRET_URL_CASES)
def test_filter_git_url_redacts_embedded_secret(
    omit_filter: OmitFilter, url: str, secret: str, expected: str
) -> None:
    filtered, detections = omit_filter.filter_git_url(url)

    assert secret not in filtered
    assert filtered == expected
    assert detections


def test_filter_git_url_redacts_query_param_token(omit_filter: OmitFilter) -> None:
    filtered, detections = omit_filter.filter_git_url(
        f"https://gitlab.com/org/repo.git?private_token={FAKE_GITLAB_PAT}"
    )

    assert "glpat-" not in filtered
    assert filtered.startswith("https://gitlab.com/org/repo.git?private_token=")
    assert detections


CLEAN_URL_CASES = [
    pytest.param("ssh://git@github.com/org/repo.git", id="ssh-git-user"),
    pytest.param("git@github.com:org/repo.git", id="scp-style"),
    pytest.param("https://github.com/org/repo.git", id="plain-https"),
    pytest.param("http://gitea.local:3000/org/repo.git", id="http-with-port"),
    pytest.param("https://host:8080/path?x=a@b", id="port-with-at-in-query"),
]


@pytest.mark.parametrize("url", CLEAN_URL_CASES)
def test_filter_git_url_leaves_credential_free_urls_unchanged(
    omit_filter: OmitFilter, url: str
) -> None:
    filtered, detections = omit_filter.filter_git_url(url)

    assert filtered == url
    assert detections == []


def test_filter_command_redacts_git_url_secrets_in_captured_commands(
    omit_filter: OmitFilter,
) -> None:
    """Captured job commands embed the same URL shapes as the git context."""
    filtered, detections = omit_filter.filter_command(
        f"git clone https://{FAKE_GITLAB_PAT}@gitlab.com/org/repo.git /work"
    )

    assert FAKE_GITLAB_PAT not in filtered
    assert "gitlab.com/org/repo.git /work" in filtered
    assert "gitlab_token" in detections


def test_filter_git_url_does_not_rematch_already_redacted_userinfo(
    omit_filter: OmitFilter,
) -> None:
    """Placeholders from earlier patterns must not be re-redacted or re-counted."""
    filtered, detections = omit_filter.filter_git_url(
        "https://user:[REDACTED]@github.com/org/repo.git"
    )

    assert filtered == "https://user:[REDACTED]@github.com/org/repo.git"
    assert detections == []


def test_filter_git_url_userinfo_respects_allowlist() -> None:
    # Allowlist patterns are tested against the matched span (the
    # "scheme://userinfo@" prefix), not the whole URL.
    allowing = OmitFilter({"allowlist": {"patterns": [r"https://myorg@"]}})

    filtered, detections = allowing.filter_git_url(
        "https://myorg@dev.azure.com/myorg/proj/_git/repo"
    )

    assert filtered == "https://myorg@dev.azure.com/myorg/proj/_git/repo"
    assert detections == []
