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


def test_scope_list_renders_owner_project_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_logged_in_scope_context(monkeypatch)

    result = CliRunner().invoke(scope, ["list", "--glaas-api-url", "http://glaas.test"])

    assert result.exit_code == 0, result.output
    assert "acme/foundation-models" in result.output
    assert "acme/readonly-project" in result.output
    assert "read-only" in result.output
    assert "proj-789" not in result.output


def test_scope_use_owner_project_name_writes_project_binding(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")
    monkeypatch.chdir(tmp_path)
    _patch_logged_in_scope_context(monkeypatch)

    result = CliRunner().invoke(
        scope,
        ["use", "acme/foundation-models", "--glaas-api-url", "http://glaas.test"],
    )

    assert result.exit_code == 0, result.output
    assert "Set roar scope to acme/foundation-models" in result.output
    config_text = (roar_dir / "config.toml").read_text(encoding="utf-8")
    assert 'owner_id = "org-456"' in config_text
    assert 'owner_type = "organization"' in config_text
    assert 'project_id = "proj-789"' in config_text
    resolved = load_repo_scope(tmp_path)
    assert resolved is not None
    assert resolved.mode == "project"
    assert resolved.project_id == "proj-789"


def test_scope_config_reads_project_visibility_from_treqs_binding(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-1"\nowner_type = "organization"\n'
        'project_id = "proj-1"\nvisibility = "public"\n',
        encoding="utf-8",
    )

    resolved = load_repo_scope(tmp_path)

    assert resolved is not None
    assert resolved.mode == "project"
    assert resolved.visibility == "public"


def test_scope_config_visibility_defaults_none_when_absent(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        '[treqs]\nowner_id = "owner-1"\nowner_type = "organization"\nproject_id = "proj-1"\n',
        encoding="utf-8",
    )

    resolved = load_repo_scope(tmp_path)

    assert resolved is not None
    assert resolved.mode == "project"
    assert resolved.visibility is None


def test_scope_use_public_project_persists_visibility(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")
    monkeypatch.chdir(tmp_path)
    _patch_logged_in_scope_context(monkeypatch)

    result = CliRunner().invoke(
        scope,
        ["use", "acme/open-models", "--glaas-api-url", "http://glaas.test"],
    )

    assert result.exit_code == 0, result.output
    config_text = (roar_dir / "config.toml").read_text(encoding="utf-8")
    assert 'visibility = "public"' in config_text
    resolved = load_repo_scope(tmp_path)
    assert resolved is not None
    assert resolved.mode == "project"
    assert resolved.project_id == "proj-public"
    assert resolved.visibility == "public"


def test_scope_use_private_project_persists_visibility(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")
    monkeypatch.chdir(tmp_path)
    _patch_logged_in_scope_context(monkeypatch)

    result = CliRunner().invoke(
        scope,
        ["use", "acme/foundation-models", "--glaas-api-url", "http://glaas.test"],
    )

    assert result.exit_code == 0, result.output
    resolved = load_repo_scope(tmp_path)
    assert resolved is not None
    assert resolved.visibility == "private"


def test_scope_use_rejects_readonly_project_name(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[registration]\npublic_by_default = false\n")
    monkeypatch.chdir(tmp_path)
    _patch_logged_in_scope_context(monkeypatch)

    result = CliRunner().invoke(
        scope,
        ["use", "acme/readonly-project", "--glaas-api-url", "http://glaas.test"],
    )

    assert result.exit_code != 0
    assert "visible but not writable" in result.output


def _patch_logged_in_scope_context(monkeypatch) -> None:
    monkeypatch.setattr("roar.cli.commands.scope.load_auth_state", lambda: object())
    monkeypatch.setattr("roar.cli.commands.scope.is_auth_state_expired", lambda _state: False)
    monkeypatch.setattr(
        "roar.cli.commands.scope.resolve_auth_api_url",
        lambda value=None: value or "http://glaas.test",
    )
    monkeypatch.setattr(
        "roar.cli.commands.scope.fetch_access_context",
        lambda _url: {
            "owners": [
                {
                    "id": "user-123",
                    "type": "user",
                    "username": "trevor",
                    "display_name": "Trevor",
                },
                {
                    "id": "org-456",
                    "type": "organization",
                    "username": "acme",
                    "display_name": "Acme AI",
                },
            ],
            "projects_by_owner": {
                "org-456": [
                    {
                        "id": "proj-789",
                        "name": "foundation-models",
                        "visibility": "private",
                        "can_write": True,
                    },
                    {
                        "id": "proj-readonly",
                        "name": "readonly-project",
                        "visibility": "private",
                        "can_write": False,
                    },
                    {
                        "id": "proj-public",
                        "name": "open-models",
                        "visibility": "public",
                        "can_write": True,
                    },
                ],
                "user-123": [],
            },
        },
    )
