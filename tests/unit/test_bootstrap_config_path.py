from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from roar.core.bootstrap import _configure_core_logging
from roar.integrations.config import config_get

REPO_ROOT = Path(__file__).resolve().parents[2]


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


