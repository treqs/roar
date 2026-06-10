from __future__ import annotations

from unittest.mock import patch

from roar.core.bootstrap import bootstrap, reset
from roar.integrations import list_telemetry_providers, list_vcs_providers


def test_bootstrap_registers_builtin_integrations(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    reset()
    bootstrap(roar_dir)

    assert "git" in list_vcs_providers()
    assert "wandb" in list_telemetry_providers()


