from __future__ import annotations

from roar.core.bootstrap import bootstrap, reset
from roar.core.container import get_container


def test_bootstrap_registers_builtin_integrations(tmp_path):
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    reset()
    bootstrap(roar_dir)

    container = get_container()
    assert "git" in container.list_vcs_providers()
    assert "wandb" in container.list_telemetry_providers()
