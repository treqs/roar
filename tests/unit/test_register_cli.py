"""Unit tests for the register CLI output surface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import RegisterLineageResponse
from roar.cli.commands.register import register
from roar.integrations.config import config_set
from roar.scope_config import save_repo_scope


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def _fake_result() -> RegisterLineageResponse:
    return RegisterLineageResponse(
        success=True,
        aborted_by_user=False,
        error=None,
        session_hash="a" * 64,
        artifact_hash="b" * 64,
        jobs_registered=10,
        artifacts_registered=13,
        links_created=20,
        secrets_redacted=False,
        secrets_detected=[],
    )


def test_register_cli_prints_next_steps_for_artifacts(tmp_path: Path) -> None:
    runner = CliRunner()
    artifact_hash = "abcdef0123456789abcdef0123456789"
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        artifact_hash=artifact_hash,
        jobs_registered=3,
        artifacts_registered=4,
        links_created=5,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(register, ["model.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Registered lineage for: model.pt" in result.output
    assert "Session: 0123456789ab..." in result.output
    assert "GLaaS:" in result.output
    assert "https://glaas.example/dag/0123456789abcdef0123456789abcdef" in result.output
    assert "https://glaas.example/artifact/abcdef0123456789abcdef0123456789" in result.output
    assert "Next:" in result.output
    assert f"roar show --artifact {artifact_hash}" in result.output
    assert f"roar reproduce {artifact_hash}" in result.output


def test_register_cli_summary_reports_new_jobs_and_labels(tmp_path: Path) -> None:
    """A fresh register lists plain job counts and the synced user-label count."""
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        jobs_registered=4,
        jobs_existing=0,
        artifacts_registered=22,
        links_created=16,
        labels_synced=3,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(register, ["metrics.json", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    # Counts now ride as the note under the "lineage saved on glaas.ai" punchlist item.
    assert "4 jobs" in result.output
    assert "already registered" not in result.output
    assert "3 labels" in result.output


def test_register_cli_summary_calls_out_already_registered_jobs(tmp_path: Path) -> None:
    """A re-register shows 0 new jobs and how many were already registered."""
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        jobs_registered=0,
        jobs_existing=4,
        artifacts_registered=22,
        links_created=16,
        labels_synced=0,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(register, ["metrics.json", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "0 (4 already registered) jobs" in result.output


def test_register_cli_prefers_returned_session_url(tmp_path: Path) -> None:
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        session_url="https://glaas.example/sessions/published-session",
        jobs_registered=3,
        artifacts_registered=4,
        links_created=5,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://fallback.glaas.example",
        ),
    ):
        result = runner.invoke(register, ["model.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "https://glaas.example/sessions/published-session" in result.output
    assert (
        "https://fallback.glaas.example/dag/0123456789abcdef0123456789abcdef" not in result.output
    )


def test_register_cli_dry_run_mentions_target(tmp_path: Path) -> None:
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        jobs_registered=2,
        artifacts_registered=3,
        links_created=4,
    )

    with patch("roar.cli.commands.register.register_lineage_target", return_value=response):
        result = runner.invoke(register, ["model.pt", "--dry-run"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Dry run: would register lineage for: model.pt" in result.output
    assert "Session: 0123456789ab..." in result.output


def test_register_cli_uses_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(register, ["model.pt", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        in result.output
    )
    request = mock_register.call_args.args[0]
    assert request.public is True


def test_register_cli_private_flag_overrides_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(
            register, ["model.pt", "--yes", "--private"], obj=_mock_context(tmp_path)
        )

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        not in result.output
    )
    request = mock_register.call_args.args[0]
    assert request.public is False


def test_register_cli_anonymous_forces_public_without_default_warning(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "false", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(
            register, ["model.pt", "--yes", "--anonymous"], obj=_mock_context(tmp_path)
        )

    assert result.exit_code == 0, result.output
    assert "Warning: defaulting to public visibility" not in result.output
    request = mock_register.call_args.args[0]
    assert request.public is True
    assert request.anonymous is True


def test_register_cli_uses_anonymous_scope_as_default_publish_intent(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(register, ["model.pt", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    request = mock_register.call_args.args[0]
    assert request.public is True
    assert request.anonymous is True


def test_register_cli_prompts_before_anonymous_scope_publish(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Anonymous scope publishes publicly without account attribution." in result.output
    assert "Publish anonymously and publicly?" in result.output
    assert "Registration aborted." in result.output
    mock_register.assert_not_called()


def test_register_cli_anonymous_prompt_previews_publish_url(tmp_path: Path) -> None:
    """The confirmation prompt must show the GLaaS destination URL before
    asking the user to commit — the live prompt matches the --dry-run
    affordance so users can preview the destination before deciding.
    """
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    # Preview line should appear above the prompt copy.
    assert "Will publish to: https://glaas.ai/dag/<session-hash>" in result.output
    preview_idx = result.output.index("Will publish to:")
    prompt_idx = result.output.index("Publish anonymously and publicly?")
    assert preview_idx < prompt_idx
    mock_register.assert_not_called()


def test_register_cli_anonymous_prompt_uses_configured_glaas_host(tmp_path: Path) -> None:
    """When glaas.web_url is overridden (e.g. staging), the preview URL
    must reflect the configured host, not the public default.
    """
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)
    config_set("glaas.web_url", "https://glaas.staging.example", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Will publish to: https://glaas.staging.example/dag/<session-hash>" in result.output
    mock_register.assert_not_called()


def test_register_cli_rejects_anonymous_private(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(
            register,
            ["model.pt", "--yes", "--anonymous", "--private"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "--anonymous requires public visibility" in result.output
    mock_register.assert_not_called()


def test_register_cli_renders_warnings_above_summary(tmp_path: Path) -> None:
    """When the service returns warnings (e.g. anonymous register with a
    failed tag push), the CLI must surface them to stderr before the
    success summary — the user shouldn't have to scroll past 'Registered
    lineage for: …' to find out a sub-step degraded."""
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        artifact_hash="b" * 64,
        jobs_registered=2,
        artifacts_registered=3,
        links_created=4,
        warnings=[
            "roar tag push to git remote failed (git auth, not GLaaS) — "
            "anonymous register continued without pushing the tag.\n"
            "  The local tag exists, but viewers of the GLaaS record need it "
            "on the remote to reproduce.\n"
            "  Fix git remote auth, then push: `git push <remote> <tag>`.\n"
            "  Verbatim git error: Permission denied (publickey)"
        ],
    )

    runner = CliRunner()
    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(
            register, ["model.pt", "--yes", "--anonymous"], obj=_mock_context(tmp_path)
        )

    assert result.exit_code == 0, result.output
    # Warning prefix used (matches `roar put`'s convention).
    assert "Warning: roar tag push to git remote failed" in result.output
    # Warning appears before the "Registered lineage for:" success line.
    warning_idx = result.output.index("Warning: roar tag push to git remote failed")
    summary_idx = result.output.index("Registered lineage for:")
    assert warning_idx < summary_idx
    # Actionable info is intact in the rendered warning.
    assert "git push" in result.output
    assert "Permission denied (publickey)" in result.output


def test_register_cli_renders_already_registered(tmp_path: Path) -> None:
    """A full re-register reports a no-op clearly, not a fresh publish."""
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        already_registered=True,
        labels_synced=2,
    )
    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(register, ["model.pkl", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Already registered on GLaaS: model.pkl" in result.output
    assert "Registered lineage for:" not in result.output
    assert "Labels: 2" in result.output
    assert "https://glaas.example/dag/0123456789abcdef0123456789abcdef" in result.output


# -- reproducibility checklist (register receipt) --


def _capture_checklist(response, tmp_path: Path, unsourced=None) -> str:
    import io
    from contextlib import redirect_stdout

    from roar.cli.commands.register import _render_register_checklist

    ctx = _mock_context(tmp_path)
    buf = io.StringIO()
    with (
        patch(
            "roar.application.reproducibility.report.unsourced_input_paths",
            return_value=unsourced or [],
        ),
        redirect_stdout(buf),
    ):
        _render_register_checklist(ctx, "out", response, on_glaas=True)
    return buf.getvalue()


def _repro_response(*, reproducible=True, remote="origin"):
    from roar.application.publish.results import RegisterLineageResponse, RegisterTagSummary

    return RegisterLineageResponse(
        success=True,
        session_hash="a" * 64,
        artifact_hash="b" * 64,
        reproducible=reproducible,
        tag_summary=RegisterTagSummary(remote=remote) if remote else None,
    )


def test_checklist_all_green_shows_full_punchlist(tmp_path: Path) -> None:
    out = _capture_checklist(_repro_response(), tmp_path, unsourced=[])
    assert "Reproducibility — 5/5" in out
    assert out.count("[✅]") == 5
    # operational details fold in as notes
    assert "pushed to origin" in out


def test_checklist_flags_no_commit_and_unsourced(tmp_path: Path) -> None:
    out = _capture_checklist(
        _repro_response(reproducible=False, remote=None),
        tmp_path,
        unsourced=["/w/gen.py"],
    )
    assert "[❌] code committed to git" in out
    assert "[❌] commit reachable on a remote" in out
    assert "[❌] all inputs sourced" in out
    assert "/w/gen.py" in out
    assert "may not reproduce as recorded" in out
    # lineage is on glaas (just registered) — that box stays green
    assert "[✅] lineage saved on glaas.ai" in out


def test_checklist_is_best_effort_silent_on_error(tmp_path: Path) -> None:
    # An evaluation failure must never break registration.
    from roar.cli.commands.register import _render_register_checklist

    with patch(
        "roar.application.reproducibility.report.unsourced_input_paths",
        side_effect=RuntimeError("boom"),
    ):
        _render_register_checklist(
            _mock_context(tmp_path), "out", _repro_response(), on_glaas=True
        )  # no raise
