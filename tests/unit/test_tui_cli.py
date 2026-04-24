from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from roar.cli import cli
from roar.cli.commands import tui as tui_module


def test_tui_command_is_registered_in_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "tui" in result.output
    assert "Browse local lineage" in result.output


def test_tui_command_delegates_to_rust_binary(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "roar-tui"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    db_path = tmp_path / ".roar" / "roar.db"
    project_path = tmp_path / "project"
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert "env" in kwargs
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(tui_module, "_find_tui_binary", lambda: binary)
    monkeypatch.setattr(tui_module.subprocess, "run", fake_run)

    result = CliRunner().invoke(
        cli,
        [
            "tui",
            "--db",
            str(db_path),
            "--session",
            "ses_8g1s",
            "--job",
            "@2",
            "--artifact",
            "abc123",
            str(project_path),
        ],
    )

    assert result.exit_code == 7
    tui_calls = [call for call in calls if call and call[0] == str(binary)]
    assert tui_calls == [
        [
            str(binary),
            "--db",
            str(db_path),
            "--session",
            "ses_8g1s",
            "--job",
            "@2",
            "--artifact",
            "abc123",
            str(project_path),
        ]
    ]


def test_build_tui_args_omits_unspecified_options(tmp_path: Path) -> None:
    binary = tmp_path / "roar-tui"

    assert tui_module._build_tui_args(
        binary=binary,
        db_path=None,
        session_ref=None,
        job_ref=None,
        artifact_ref=None,
        path=None,
    ) == [str(binary)]
