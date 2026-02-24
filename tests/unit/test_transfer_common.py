"""Unit tests for shared transfer-layer git context helpers."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.services.transfer.common import resolve_git_context, resolve_repo_url_or_local_uri


def test_resolve_repo_url_or_local_uri_prefers_remote_url() -> None:
    """Existing remote URL should be returned without warning."""
    mock_vcs = MagicMock()
    mock_vcs.get_remote_url.return_value = "https://github.com/test/repo.git"
    mock_logger = MagicMock()

    resolved = resolve_repo_url_or_local_uri(mock_vcs, "/tmp/repo", logger=mock_logger)

    assert resolved == "https://github.com/test/repo.git"
    mock_logger.warning.assert_not_called()


def test_resolve_repo_url_or_local_uri_uses_file_uri_when_remote_missing(tmp_path: Path) -> None:
    """Missing remote should fall back to file:// URI and emit a warning."""
    mock_vcs = MagicMock()
    mock_vcs.get_remote_url.return_value = None
    mock_logger = MagicMock()

    resolved = resolve_repo_url_or_local_uri(mock_vcs, str(tmp_path), logger=mock_logger)

    assert resolved == tmp_path.resolve().as_uri()
    mock_logger.warning.assert_called_once()


def test_resolve_git_context_falls_back_to_local_repo_uri_when_remote_missing(
    tmp_path: Path,
) -> None:
    """resolve_git_context should use local URI fallback for repos without remotes."""
    with patch("roar.services.transfer.common.GitVCSProvider") as mock_git:
        mock_vcs = MagicMock()
        mock_vcs.get_repo_root.return_value = str(tmp_path)
        mock_vcs.get_remote_url.return_value = None
        mock_vcs.get_commit_hash.return_value = "abc123def456"
        mock_vcs.get_branch.return_value = "main"
        mock_git.return_value = mock_vcs

        context = resolve_git_context(tmp_path)

    assert context.repo == tmp_path.resolve().as_uri()
    assert context.commit == "abc123def456"
    assert context.branch == "main"
