"""Unit tests for the `roar label` CLI surface (Tasks B and D of
tb/label-sync-safety).

These tests exercise the Click command layer directly (flag plumbing, error
rendering) with the application layer mocked out; the underlying confirmation
gate and deletion-baseline logic are covered in
tests/application/query/test_label.py, and full end-to-end behavior against a
fake GLaaS server is covered in tests/integration/test_label_sync_cli_integration.py
and tests/integration/test_label_remote_cli_integration.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.query.requests import LabelSyncRequest
from roar.cli.commands.label import label_set, label_sync


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.repo_root = tmp_path
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_label_sync_yes_flag_sets_skip_confirmation(tmp_path: Path) -> None:
    """Task B: `-y`/`--yes` plumbs through to LabelSyncRequest.skip_confirmation."""
    runner = CliRunner()
    with patch("roar.cli.commands.label.sync_labels", return_value="Synced.") as sync:
        result = runner.invoke(
            label_sync,
            ["artifact", "processed.csv", "--yes"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    sent_request = sync.call_args.args[0]
    assert isinstance(sent_request, LabelSyncRequest)
    assert sent_request.skip_confirmation is True


def test_label_sync_short_yes_flag_sets_skip_confirmation(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("roar.cli.commands.label.sync_labels", return_value="Synced.") as sync:
        result = runner.invoke(
            label_sync,
            ["artifact", "processed.csv", "-y"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert sync.call_args.args[0].skip_confirmation is True


def test_label_sync_defaults_to_not_skipping_confirmation(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("roar.cli.commands.label.sync_labels", return_value="Synced.") as sync:
        result = runner.invoke(
            label_sync,
            ["artifact", "processed.csv"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert sync.call_args.args[0].skip_confirmation is False


def test_label_sync_declined_deletion_prompt_exits_cleanly_without_traceback(
    tmp_path: Path,
) -> None:
    """Task B: when the (mocked) application layer aborts a declined deletion
    prompt via SystemExit, the CLI must not wrap it into a ClickException or
    let a traceback leak — it should just propagate the clean exit."""
    runner = CliRunner()
    with patch("roar.cli.commands.label.sync_labels", side_effect=SystemExit(1)):
        result = runner.invoke(
            label_sync,
            ["artifact", "processed.csv"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_label_sync_value_error_becomes_clean_click_exception(tmp_path: Path) -> None:
    """Sanity check: ValueError from the application layer renders as a
    friendly `Error: ...` message, not a raw traceback."""
    runner = CliRunner()
    with patch(
        "roar.cli.commands.label.sync_labels",
        side_effect=ValueError("No local user-managed labels or label deletions to sync."),
    ):
        result = runner.invoke(
            label_sync,
            ["artifact", "processed.csv"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "No local user-managed labels" in result.output


def test_remote_label_set_conflict_error_surfaces_cleanly_not_as_a_traceback(
    tmp_path: Path,
) -> None:
    """Task D: a 409 conflict from the --remote edit path (mapped by
    _map_remote_label_error to a "conflicted with a concurrent edit... retry"
    message) must reach the user as a clean CLI error, not a raw traceback."""
    runner = CliRunner()
    conflict_message = (
        "Remote label edit conflicted with a concurrent edit: HTTP 409: version mismatch. "
        "Retry to apply the change against the latest version."
    )
    with patch(
        "roar.cli.commands.label.remote_set_labels",
        side_effect=ValueError(conflict_message),
    ):
        result = runner.invoke(
            label_set,
            ["--remote", "dag", "a" * 64, "team=cv"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "conflicted with a concurrent edit" in result.output
    assert "Retry" in result.output
