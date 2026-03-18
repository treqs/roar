from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from roar.core.bootstrap import _configure_core_logging
from roar.integrations.config import config_get

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_configure_core_logging_uses_explicit_roar_dir(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    roar_dir = repo / ".roar"
    roar_dir.mkdir(parents=True)
    (roar_dir / "config.toml").write_text(
        '[logging]\nlevel = "debug"\nconsole = true\nfile = false\n',
        encoding="utf-8",
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with patch("roar.core.bootstrap.configure_logger") as configure_logger:
        _configure_core_logging(roar_dir)

    configure_logger.assert_called_once_with(
        level="debug",
        console_enabled=True,
        file_enabled=False,
    )


def test_bootstrap_does_not_import_execution_registry_for_core_logging(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roar_dir = repo / ".roar"
    roar_dir.mkdir(parents=True)
    (roar_dir / "config.toml").write_text('[logging]\nlevel = "info"\n', encoding="utf-8")

    env = os.environ.copy()
    pythonpath_entries = [str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path

from roar.core.bootstrap import bootstrap, reset

roar_dir = Path(sys.argv[1])
reset()
bootstrap(roar_dir)
assert "roar.execution.framework.registry" not in sys.modules
assert "roar.backends.ray.plugin" not in sys.modules
print("ok")
""",
            str(roar_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_config_get_returns_nested_model_as_dict(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "[registration.omit]\nenabled = false\n",
        encoding="utf-8",
    )

    omit_config = config_get("registration.omit", start_dir=str(tmp_path))

    assert isinstance(omit_config, dict)
    assert omit_config["enabled"] is False


def test_config_get_core_key_does_not_import_execution_registry(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text('[logging]\nlevel = "debug"\n', encoding="utf-8")

    env = os.environ.copy()
    pythonpath_entries = [str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from roar.integrations.config import config_get

value = config_get("logging.level", start_dir=sys.argv[1])
assert value == "debug"
assert "roar.execution.framework.registry" not in sys.modules
assert "roar.backends.ray.plugin" not in sys.modules
print("ok")
""",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
