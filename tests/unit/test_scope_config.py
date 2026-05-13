from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from roar.cli.commands.scope import scope
from roar.scope_config import clear_repo_scope, load_repo_scope, save_repo_scope


def test_scope_config_round_trips_private_mode(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")

    path = save_repo_scope("private", start_dir=tmp_path)

    assert path == roar_dir / "config.toml"
    resolved = load_repo_scope(tmp_path)
    assert resolved is not None
    assert resolved.mode == "private"
    assert resolved.source == "scope"


def test_scope_config_treats_legacy_treqs_binding_as_project_scope(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-1"\nowner_type = "organization"\nproject_id = "proj-1"\n',
        encoding="utf-8",
    )

    resolved = load_repo_scope(tmp_path)

    assert resolved is not None
    assert resolved.mode == "project"
    assert resolved.source == "legacy_treqs"
    assert resolved.owner_id == "owner-1"
    assert resolved.owner_type == "organization"
    assert resolved.project_id == "proj-1"


def test_clear_repo_scope_removes_scope_table(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        '[scope]\nmode = "public"\n\n[registration]\npublic_by_default = false\n',
        encoding="utf-8",
    )

    clear_repo_scope(start_dir=tmp_path)

    assert load_repo_scope(tmp_path) is None
    assert "[registration]" in (roar_dir / "config.toml").read_text(encoding="utf-8")


def test_scope_use_private_writes_repo_scope(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(scope, ["use", "private"])

    assert result.exit_code == 0, result.output
    assert "Set roar scope to private" in result.output
    resolved = load_repo_scope(tmp_path)
    assert resolved is not None
    assert resolved.mode == "private"
