"""Tests for proxy configuration model and config get/set."""

from types import SimpleNamespace
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

        with patch("roar.integrations.config.access.load_settings") as mock_load:
            mock_load.return_value = SimpleNamespace(proxy=SimpleNamespace(enabled=False))
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


class _SettingsStub:
    """Bare-attribute stub that mimics RoarSettings for the ``config_get``
    lookup path. Uses real attributes (not MagicMock) so ``hasattr``
    returns the right answer for missing keys."""

    def __init__(
        self,
        *,
        backend_config_source: dict,
        to_dict_result: dict | None = None,
        to_dict_must_not_be_called: bool = False,
    ) -> None:
        self._backend_config_source = backend_config_source
        self._to_dict_result = to_dict_result
        self._to_dict_must_not_be_called = to_dict_must_not_be_called
        self.to_dict_call_count = 0

    def to_dict(self) -> dict:
        self.to_dict_call_count += 1
        if self._to_dict_must_not_be_called:
            raise AssertionError(
                "to_dict() must not be called for non-backend keys — it "
                "would trigger ~300ms of backend discovery on every "
                "roar invocation."
            )
        return self._to_dict_result or {}


class TestConfigGetDoesNotDiscoverBackends:
    """``config_get`` runs on every roar invocation (e.g. for
    ``hints.enabled`` from the shared output gate). Eagerly resolving
    ``to_dict()`` would import every backend plugin module just to know
    a non-backend key isn't backend-namespaced. The fast path must
    short-circuit before reaching ``to_dict()`` for keys that can't
    resolve through a backend.
    """

    def test_non_backend_key_skips_to_dict(self) -> None:
        from roar.integrations.config import config_get

        settings = _SettingsStub(
            backend_config_source={},
            to_dict_must_not_be_called=True,
        )

        with patch("roar.integrations.config.access.load_settings", return_value=settings):
            result = config_get("hints.enabled")

        assert result is None
        assert settings.to_dict_call_count == 0

    def test_raw_toml_value_returned_without_to_dict(self) -> None:
        """When the user has explicitly set a non-typed key in TOML
        (e.g. ``[hints] enabled = false``), the raw-config walk must
        return it — also without touching ``to_dict()``.
        """
        from roar.integrations.config import config_get

        settings = _SettingsStub(
            backend_config_source={"hints": {"enabled": False}},
            to_dict_must_not_be_called=True,
        )

        with patch("roar.integrations.config.access.load_settings", return_value=settings):
            result = config_get("hints.enabled")

        assert result is False
        assert settings.to_dict_call_count == 0

    def test_backend_key_still_resolves_through_to_dict(self) -> None:
        """For known backend sections, ``to_dict()`` is still the
        authoritative source — that's where the backend's defaults live
        when the user hasn't overridden them. Don't accidentally break
        the slow path for keys that need it.
        """
        from roar.integrations.config import config_get

        settings = _SettingsStub(
            backend_config_source={},
            to_dict_result={"ray": {"runtime": "auto"}},
        )

        with patch("roar.integrations.config.access.load_settings", return_value=settings):
            result = config_get("ray.runtime")

        assert result == "auto"
        assert settings.to_dict_call_count == 1


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
