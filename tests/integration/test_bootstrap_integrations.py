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


def test_bootstrap_delegates_integration_wiring_to_integrations_layer(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    reset()
    with patch("roar.core.bootstrap.bootstrap_integrations") as bootstrap_integrations:
        bootstrap(roar_dir)

    bootstrap_integrations.assert_called_once_with()
