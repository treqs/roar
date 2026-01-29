"""
Unit tests for git URL utilities.

Tests SSH URL detection and SSH-to-HTTPS conversion.
"""

import pytest

from roar.utils.git_url import is_ssh_url, normalize_git_url, ssh_to_https, urls_match


class TestIsSshUrl:
    """Tests for is_ssh_url function."""

    def test_scp_format_github(self):
        """SCP-like format for GitHub should return True."""
        assert is_ssh_url("git@github.com:user/repo.git") is True

    def test_scp_format_gitlab(self):
        """SCP-like format for GitLab should return True."""
        assert is_ssh_url("git@gitlab.com:group/repo.git") is True

    def test_scp_format_nested_path(self):
        """SCP-like format with nested path should return True."""
        assert is_ssh_url("git@gitlab.com:group/subgroup/repo.git") is True

    def test_ssh_scheme_github(self):
        """Explicit SSH scheme for GitHub should return True."""
        assert is_ssh_url("ssh://git@github.com/user/repo.git") is True

    def test_ssh_scheme_gitlab(self):
        """Explicit SSH scheme for GitLab should return True."""
        assert is_ssh_url("ssh://git@gitlab.com/group/repo.git") is True

    def test_ssh_scheme_self_hosted(self):
        """SSH scheme for self-hosted should return True."""
        assert is_ssh_url("ssh://git@git.company.com/team/repo.git") is True

    def test_https_url_returns_false(self):
        """HTTPS URL should return False."""
        assert is_ssh_url("https://github.com/user/repo.git") is False

    def test_http_url_returns_false(self):
        """HTTP URL should return False."""
        assert is_ssh_url("http://github.com/user/repo.git") is False

    def test_file_path_returns_false(self):
        """File path should return False."""
        assert is_ssh_url("/path/to/repo.git") is False

    def test_empty_string_returns_false(self):
        """Empty string should return False."""
        assert is_ssh_url("") is False


class TestSshToHttps:
    """Tests for ssh_to_https function."""

    def test_scp_format_github(self):
        """Convert SCP-like GitHub URL to HTTPS."""
        result = ssh_to_https("git@github.com:user/repo.git")
        assert result == "https://github.com/user/repo.git"

    def test_scp_format_gitlab(self):
        """Convert SCP-like GitLab URL to HTTPS."""
        result = ssh_to_https("git@gitlab.com:group/repo.git")
        assert result == "https://gitlab.com/group/repo.git"

    def test_scp_format_nested_path(self):
        """Convert SCP-like URL with nested path to HTTPS."""
        result = ssh_to_https("git@gitlab.com:group/subgroup/repo.git")
        assert result == "https://gitlab.com/group/subgroup/repo.git"

    def test_scp_format_bitbucket(self):
        """Convert SCP-like Bitbucket URL to HTTPS."""
        result = ssh_to_https("git@bitbucket.org:team/repo.git")
        assert result == "https://bitbucket.org/team/repo.git"

    def test_scp_format_self_hosted(self):
        """Convert SCP-like self-hosted URL to HTTPS."""
        result = ssh_to_https("git@git.company.com:team/project.git")
        assert result == "https://git.company.com/team/project.git"

    def test_ssh_scheme_github(self):
        """Convert SSH scheme GitHub URL to HTTPS."""
        result = ssh_to_https("ssh://git@github.com/user/repo.git")
        assert result == "https://github.com/user/repo.git"

    def test_ssh_scheme_gitlab(self):
        """Convert SSH scheme GitLab URL to HTTPS."""
        result = ssh_to_https("ssh://git@gitlab.com/group/repo.git")
        assert result == "https://gitlab.com/group/repo.git"

    def test_ssh_scheme_nested_path(self):
        """Convert SSH scheme URL with nested path to HTTPS."""
        result = ssh_to_https("ssh://git@gitlab.com/group/subgroup/repo.git")
        assert result == "https://gitlab.com/group/subgroup/repo.git"

    def test_ssh_scheme_self_hosted(self):
        """Convert SSH scheme self-hosted URL to HTTPS."""
        result = ssh_to_https("ssh://git@git.company.com/team/repo.git")
        assert result == "https://git.company.com/team/repo.git"

    def test_https_url_returns_none(self):
        """HTTPS URL should return None."""
        assert ssh_to_https("https://github.com/user/repo.git") is None

    def test_http_url_returns_none(self):
        """HTTP URL should return None."""
        assert ssh_to_https("http://github.com/user/repo.git") is None

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        assert ssh_to_https("") is None

    def test_file_path_returns_none(self):
        """File path should return None."""
        assert ssh_to_https("/path/to/repo.git") is None

    def test_no_dot_git_suffix(self):
        """URL without .git suffix should still convert."""
        result = ssh_to_https("git@github.com:user/repo")
        assert result == "https://github.com/user/repo"


class TestNormalizeGitUrl:
    """Tests for normalize_git_url function."""

    def test_scp_format(self):
        assert normalize_git_url("git@github.com:user/repo.git") == "github.com/user/repo"

    def test_scp_format_no_suffix(self):
        assert normalize_git_url("git@github.com:user/repo") == "github.com/user/repo"

    def test_https_with_suffix(self):
        assert normalize_git_url("https://github.com/user/repo.git") == "github.com/user/repo"

    def test_https_no_suffix(self):
        assert normalize_git_url("https://github.com/user/repo") == "github.com/user/repo"

    def test_ssh_scheme(self):
        assert normalize_git_url("ssh://git@github.com/user/repo.git") == "github.com/user/repo"

    def test_ssh_scheme_no_suffix(self):
        assert normalize_git_url("ssh://git@github.com/user/repo") == "github.com/user/repo"

    def test_http(self):
        assert normalize_git_url("http://github.com/user/repo.git") == "github.com/user/repo"


class TestUrlsMatch:
    """Tests for urls_match function."""

    def test_ssh_vs_https(self):
        assert urls_match(
            "git@github.com:user/repo.git",
            "https://github.com/user/repo.git",
        )

    def test_ssh_vs_https_no_suffix(self):
        assert urls_match(
            "git@github.com:user/repo",
            "https://github.com/user/repo",
        )

    def test_mixed_suffix(self):
        assert urls_match(
            "git@github.com:user/repo.git",
            "https://github.com/user/repo",
        )

    def test_ssh_scheme_vs_https(self):
        assert urls_match(
            "ssh://git@github.com/user/repo.git",
            "https://github.com/user/repo.git",
        )

    def test_different_repos_no_match(self):
        assert not urls_match(
            "git@github.com:user/repo1.git",
            "git@github.com:user/repo2.git",
        )

    def test_different_hosts_no_match(self):
        assert not urls_match(
            "git@github.com:user/repo.git",
            "git@gitlab.com:user/repo.git",
        )
