"""Product-path coverage for `roar proxy` configuration commands."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_proxy_enable_disable_round_trip_updates_status_and_config(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    config_path = temp_git_repo / ".roar" / "config.toml"

    initial_status = roar_cli("proxy")
    assert initial_status.returncode == 0
    assert "Proxy enabled: False" in initial_status.stdout

    enable_result = roar_cli("proxy", "enable")
    assert enable_result.returncode == 0
    assert "S3 proxy enabled for roar run." in enable_result.stdout
    assert str(config_path) in enable_result.stdout
    enabled_config = config_path.read_text(encoding="utf-8")
    assert "[proxy]" in enabled_config
    assert "enabled = true" in enabled_config

    status_after_enable = roar_cli("proxy", "status")
    assert status_after_enable.returncode == 0
    assert "Proxy enabled: True" in status_after_enable.stdout

    disable_result = roar_cli("proxy", "disable")
    assert disable_result.returncode == 0
    assert "S3 proxy disabled." in disable_result.stdout
    assert str(config_path) in disable_result.stdout
    disabled_config = config_path.read_text(encoding="utf-8")
    assert "[proxy]" in disabled_config
    assert "enabled = false" in disabled_config

    status_after_disable = roar_cli("proxy")
    assert status_after_disable.returncode == 0
    assert "Proxy enabled: False" in status_after_disable.stdout
