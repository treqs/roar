from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.query import ShowQueryRequest
from roar.cli.commands.log import log
from roar.cli.commands.show import show
from roar.cli.commands.status import status


def _ctx(tmp_path):
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.roar_dir.mkdir()
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_log_cli_exits_non_zero_without_active_session(tmp_path) -> None:
    result = CliRunner().invoke(log, obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No active session." in result.output


def test_status_cli_exits_non_zero_without_active_session(tmp_path) -> None:
    result = CliRunner().invoke(status, obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No active session." in result.output


def test_show_cli_exits_non_zero_for_missing_path_lookup(tmp_path) -> None:
    result = CliRunner().invoke(show, ["artifact.bin"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "No artifact found for path: artifact.bin" in result.output


def test_show_cli_path_selector_builds_explicit_request(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    # `--path deadbeef` resolves to an artifact; render_show_with_summary
    # returns (text, summary). The mock summary is a ShowSessionSummary
    # so we don't trigger the artifact-only reproduce hint that the show
    # CLI now appends.
    from roar.application.query.results import ShowSessionSummary

    fake_summary = ShowSessionSummary(hash="a" * 64, created_at=0)
    with patch(
        "roar.cli.commands.show.render_show_with_summary",
        return_value=("ok", fake_summary),
    ) as render_show:
        result = CliRunner().invoke(show, ["--path", "deadbeef"], obj=ctx)

    assert result.exit_code == 0, result.output
    # Brand header now goes to stderr (agent/CI visibility); stdout
    # carries just the rendered query result so pipelines stay clean.
    assert result.stdout == "ok\n"
    render_show.assert_called_once_with(
        ShowQueryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, ref="deadbeef", selector="path")
    )


def test_show_cli_rejects_multiple_explicit_selectors(tmp_path) -> None:
    result = CliRunner().invoke(show, ["--path", "a", "--job", "@1"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Specify only one of --path, --job, --artifact, or --session." in result.output


def test_show_cli_rejects_positional_ref_with_explicit_selector(tmp_path) -> None:
    result = CliRunner().invoke(show, ["--artifact", "deadbeef", "other"], obj=_ctx(tmp_path))

    assert result.exit_code == 2
    assert "Positional REF cannot be combined" in result.output


def test_show_cli_emits_reproduce_hint_for_artifact_only(tmp_path, monkeypatch) -> None:
    """`roar show <artifact-hash>` ends with a `hint: To reproduce …`
    line. `roar show @1` (job) and `roar show --session` do not — the
    reproduce verb takes an artifact, not a job."""
    from roar.application.query.results import (
        ShowArtifactSummary,
        ShowJobSummary,
        ShowSessionSummary,
    )
    from roar.cli import _format

    monkeypatch.setattr(_format, "hints_should_print", lambda: True)
    ctx = _ctx(tmp_path)

    artifact_summary = ShowArtifactSummary(id="abc123def456", size=42, first_seen_at=0)
    with patch(
        "roar.cli.commands.show.render_show_with_summary",
        return_value=("artifact output", artifact_summary),
    ):
        result = CliRunner().invoke(show, ["--artifact", "abc123"], obj=ctx)
    assert result.exit_code == 0
    assert "To reproduce this artifact: roar reproduce abc123def456" in result.output

    job_summary = ShowJobSummary(
        job_uid="abc12345", timestamp=0, duration_seconds=0, exit_code=0, command="x"
    )
    with patch(
        "roar.cli.commands.show.render_show_with_summary",
        return_value=("job output", job_summary),
    ):
        result = CliRunner().invoke(show, ["--job", "@1"], obj=ctx)
    assert result.exit_code == 0
    assert "reproduce" not in result.output

    session_summary = ShowSessionSummary(hash="a" * 64, created_at=0)
    with patch(
        "roar.cli.commands.show.render_show_with_summary",
        return_value=("session output", session_summary),
    ):
        result = CliRunner().invoke(show, ["--session"], obj=ctx)
    assert result.exit_code == 0
    assert "reproduce" not in result.output
