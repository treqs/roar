from pathlib import Path
from unittest.mock import patch

from roar.cli.context import RoarContext


def test_create_resolves_parent_roar_dir(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    roar_dir = root / ".roar"
    roar_dir.mkdir(parents=True)
    nested.mkdir(parents=True)

    with (
        patch.object(RoarContext, "_get_repo_root", return_value=root),
        patch.object(RoarContext, "_load_config", return_value={"output": {"quiet": True}}),
    ):
        ctx = RoarContext.create(cwd=nested)
        # Access config inside mock scope (lazy-loaded on first access)
        config = ctx.config

    assert ctx.roar_dir == roar_dir
    assert ctx.is_initialized is True
    assert config.get("output", {}).get("quiet") is True


def test_create_falls_back_to_cwd_roar_dir_when_uninitialized(tmp_path: Path) -> None:
    cwd = tmp_path / "repo" / "nested"
    cwd.mkdir(parents=True)

    with (
        patch.object(RoarContext, "_get_repo_root", return_value=None),
        patch.object(RoarContext, "_load_config", return_value={}),
    ):
        ctx = RoarContext.create(cwd=cwd)

    assert ctx.roar_dir == cwd / ".roar"
    assert ctx.is_initialized is False
