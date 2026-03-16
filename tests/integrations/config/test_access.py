"""Tests for proxy configuration model and config get/set."""

from unittest.mock import patch

from roar.integrations.config import (
    CONFIGURABLE_KEYS,
    ProxyConfig,
    RoarConfig,
    config_get,
    save_config,
)


class TestProxyConfigModel:
    def test_default_enabled_is_false(self):
        config = ProxyConfig()
        assert config.enabled is False

    def test_roar_config_includes_proxy(self):
        config = RoarConfig()
        assert hasattr(config, "proxy")
        assert isinstance(config.proxy, ProxyConfig)

    def test_to_dict_includes_proxy(self):
        config = RoarConfig()
        d = config.to_dict()
        assert "proxy" in d
        assert d["proxy"]["enabled"] is False


class TestProxyConfigurableKeys:
    def test_proxy_enabled_in_configurable_keys(self):
        assert "proxy.enabled" in CONFIGURABLE_KEYS

    def test_proxy_enabled_metadata(self):
        key_info = CONFIGURABLE_KEYS["proxy.enabled"]
        assert key_info["type"] is bool
        assert key_info["default"] is False
        assert "description" in key_info


class TestProxyConfigGetSet:
    def test_config_get_returns_false_by_default(self, tmp_path):
        # Create a minimal .roar directory with no config file
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with patch("roar.integrations.config.access.load_config") as mock_load:
            mock_load.return_value = RoarConfig().to_dict()
            result = config_get("proxy.enabled")
        assert result is False

    def test_config_set_can_enable(self, tmp_path):
        from roar.integrations.config import config_set

        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()
        config_path = roar_dir / "config.toml"

        with (
            patch("roar.integrations.config.access.load_config") as mock_load,
            patch(
                "roar.integrations.config.access.get_config_path_for_write",
                return_value=config_path,
            ),
            patch("roar.integrations.config.access.save_config") as mock_save,
        ):
            mock_load.return_value = RoarConfig().to_dict()
            _path, value = config_set("proxy.enabled", "true")

        assert value is True
        mock_save.assert_called_once()
        saved_config = mock_save.call_args[0][0]
        assert saved_config["proxy"]["enabled"] is True


class TestSaveConfigProxy:
    def test_save_config_writes_proxy_section_when_non_default(self, tmp_path):
        config = RoarConfig().to_dict()
        config["proxy"]["enabled"] = True
        config_path = tmp_path / "config.toml"

        save_config(config, config_path)

        content = config_path.read_text()
        assert "[proxy]" in content
        assert "enabled = true" in content

    def test_save_config_omits_proxy_section_when_default(self, tmp_path):
        config = RoarConfig().to_dict()
        config_path = tmp_path / "config.toml"

        save_config(config, config_path)

        content = config_path.read_text()
        assert "[proxy]" not in content
