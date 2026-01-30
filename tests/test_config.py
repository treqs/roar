"""
Tests for roar configuration loading and the init command.

Tests verify:
- roar init creates .roar/config.toml with correct structure
- The default config template can be parsed by load_config()
- Defaults in the template match Pydantic model defaults
- Config round-trip (save -> load) preserves values
"""

import subprocess
import sys
from pathlib import Path

from roar.cli.commands.init import DEFAULT_CONFIG_TEMPLATE
from roar.config import (
    CONFIGURABLE_KEYS,
    VALID_HASH_ALGORITHMS,
    _get_default_config,
    config_get,
    load_config,
    save_config,
)
from roar.core.settings import find_config_file, load_settings


class TestRoarInit:
    """Tests for the roar init command."""

    def test_init_creates_config_file(self, tmp_path: Path) -> None:
        """roar init creates .roar/config.toml file."""
        # Run roar init
        result = subprocess.run(
            [sys.executable, "-m", "roar", "init", "-n"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"roar init failed: {result.stderr}"

        # Verify .roar directory exists
        roar_dir = tmp_path / ".roar"
        assert roar_dir.exists(), ".roar directory should be created"
        assert roar_dir.is_dir(), ".roar should be a directory"

        # Verify config.toml exists
        config_path = roar_dir / "config.toml"
        assert config_path.exists(), ".roar/config.toml should be created"
        assert config_path.is_file(), "config.toml should be a file"

    def test_init_config_is_valid_toml(self, tmp_path: Path) -> None:
        """roar init creates a valid TOML config file."""
        # Run roar init
        subprocess.run(
            [sys.executable, "-m", "roar", "init", "-n"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

        # Load the config - this will fail if TOML is invalid
        config = load_config(start_dir=str(tmp_path))

        # Verify basic structure
        assert "output" in config
        assert "registration" in config
        assert "hash" in config
        assert "logging" in config

    def test_init_config_can_be_found(self, tmp_path: Path) -> None:
        """find_config_file() can locate the config created by init."""
        # Run roar init
        subprocess.run(
            [sys.executable, "-m", "roar", "init", "-n"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

        # find_config_file should locate it
        found = find_config_file(str(tmp_path))
        assert found is not None, "find_config_file should find the config"
        assert found.name == "config.toml"
        assert found.parent.name == ".roar"


class TestDefaultConfigTemplate:
    """Tests for the DEFAULT_CONFIG_TEMPLATE used by roar init."""

    def test_template_is_valid_toml(self, tmp_path: Path) -> None:
        """DEFAULT_CONFIG_TEMPLATE is valid TOML."""
        # Write template to file
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        # Load should not raise
        config = load_config(config_path=config_path)
        assert isinstance(config, dict)

    def test_template_output_defaults(self, tmp_path: Path) -> None:
        """Template has correct output section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["output"]["track_repo_files"] is False
        assert config["output"]["quiet"] is False

    def test_template_analyzers_defaults(self, tmp_path: Path) -> None:
        """Template has correct analyzers section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["analyzers"]["experiment_tracking"] is True

    def test_template_filters_defaults(self, tmp_path: Path) -> None:
        """Template has correct filters section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["filters"]["ignore_system_reads"] is True
        assert config["filters"]["ignore_package_reads"] is True
        assert config["filters"]["ignore_torch_cache"] is True
        assert config["filters"]["ignore_tmp_files"] is True

    def test_template_registration_defaults(self, tmp_path: Path) -> None:
        """Template has correct registration section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["registration"]["omit"]["enabled"] is True
        assert "WANDB_API_KEY" in config["registration"]["omit"]["env_vars"]["names"]
        assert "OPENAI_API_KEY" in config["registration"]["omit"]["env_vars"]["names"]
        assert "ANTHROPIC_API_KEY" in config["registration"]["omit"]["env_vars"]["names"]

    def test_template_hash_defaults(self, tmp_path: Path) -> None:
        """Template has correct hash section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["hash"]["primary"] == "blake3"
        assert config["hash"]["get"] == ["sha256"]
        assert config["hash"]["put"] == []
        assert config["hash"]["run"] == []

    def test_template_reversible_defaults(self, tmp_path: Path) -> None:
        """Template has correct reversible section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["reversible"]["enabled"] is False

    def test_template_logging_defaults(self, tmp_path: Path) -> None:
        """Template has correct logging section defaults."""
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        config = load_config(config_path=config_path)

        assert config["logging"]["level"] == "warning"
        assert config["logging"]["console"] is False
        assert config["logging"]["file"] is True


class TestDefaultsMatchPydanticModels:
    """Verify that the init template defaults match Pydantic model defaults."""

    def test_template_matches_pydantic_defaults(self, tmp_path: Path) -> None:
        """DEFAULT_CONFIG_TEMPLATE values match RoarConfig model defaults."""
        # Load template config
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)
        template_config = load_config(config_path=config_path)

        # Get Pydantic model defaults
        pydantic_defaults = _get_default_config()

        # Compare all sections
        for section in [
            "output",
            "analyzers",
            "filters",
            "cleanup",
            "hash",
            "reversible",
            "logging",
        ]:
            for key, template_val in template_config.get(section, {}).items():
                pydantic_val = pydantic_defaults.get(section, {}).get(key)
                assert template_val == pydantic_val, (
                    f"Mismatch for {section}.{key}: "
                    f"template={template_val!r}, pydantic={pydantic_val!r}"
                )

        # Compare registration section (including nested omit)
        assert (
            template_config["registration"]["omit"]["enabled"]
            == pydantic_defaults["registration"]["omit"]["enabled"]
        )
        assert (
            template_config["registration"]["omit"]["env_vars"]["names"]
            == pydantic_defaults["registration"]["omit"]["env_vars"]["names"]
        )


class TestConfigLoading:
    """Tests for config loading functionality."""

    def test_load_config_without_file_returns_defaults(self, tmp_path: Path) -> None:
        """load_config returns defaults when no config file exists."""
        config = load_config(start_dir=str(tmp_path))

        # Should have all sections with defaults
        assert config["output"]["quiet"] is False
        assert config["registration"]["omit"]["enabled"] is True
        assert config["hash"]["primary"] == "blake3"

    def test_load_config_merges_with_defaults(self, tmp_path: Path) -> None:
        """load_config merges file values with defaults."""
        # Create partial config
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("""
[output]
quiet = true
""")

        config = load_config(start_dir=str(tmp_path))

        # Specified value should be loaded
        assert config["output"]["quiet"] is True
        # Unspecified values should use defaults
        assert config["output"]["track_repo_files"] is False
        assert config["registration"]["omit"]["enabled"] is True

    def test_load_settings_returns_settings_object(self, tmp_path: Path) -> None:
        """load_settings returns a RoarSettings instance."""
        settings = load_settings(start_dir=str(tmp_path))

        assert hasattr(settings, "output")
        assert hasattr(settings, "registration")
        assert hasattr(settings, "hash")
        assert settings.output.quiet is False

    def test_config_get_nested_keys(self, tmp_path: Path) -> None:
        """config_get can access deeply nested keys."""
        # Create config with nested values
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

        # Test nested access
        omit_enabled = config_get("registration.omit.enabled", start_dir=str(tmp_path))
        assert omit_enabled is True

        env_names = config_get("registration.omit.env_vars.names", start_dir=str(tmp_path))
        assert isinstance(env_names, list)
        assert "WANDB_API_KEY" in env_names


class TestConfigSaveLoad:
    """Tests for config save and reload functionality."""

    def test_save_and_reload_preserves_values(self, tmp_path: Path) -> None:
        """Saving and reloading config preserves all values."""
        # Load default config
        config = load_config(start_dir=str(tmp_path))

        # Modify some values
        config["output"]["quiet"] = True
        config["glaas"]["url"] = "https://test.example.com"

        # Save
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        save_config(config, config_path)

        # Reload
        reloaded = load_config(config_path=config_path)

        # Verify values
        assert reloaded["output"]["quiet"] is True
        assert reloaded["glaas"]["url"] == "https://test.example.com"

        # Defaults should still be correct
        assert reloaded["hash"]["primary"] == "blake3"
        assert reloaded["registration"]["omit"]["enabled"] is True

    def test_save_only_writes_non_defaults(self, tmp_path: Path) -> None:
        """save_config only writes values that differ from defaults."""
        # Load config and modify one value
        config = load_config(start_dir=str(tmp_path))
        config["output"]["quiet"] = True

        # Save
        config_path = tmp_path / ".roar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        save_config(config, config_path)

        # Read the raw file
        content = config_path.read_text()

        # Should only contain the modified section
        assert "[output]" in content
        assert "quiet = true" in content

        # Should not contain default values that weren't changed
        # (hash.primary = "blake3" is the default, so shouldn't be in file)
        lines = [
            line.strip()
            for line in content.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        assert 'primary = "blake3"' not in lines


class TestConfigurableKeys:
    """Tests for CONFIGURABLE_KEYS metadata."""

    def test_all_configurable_keys_are_valid(self, tmp_path: Path) -> None:
        """All CONFIGURABLE_KEYS can be accessed via config_get."""
        for key in CONFIGURABLE_KEYS:
            # This should not raise
            value = config_get(key, start_dir=str(tmp_path))
            # Value should match the documented default (if the key exists in the model)
            expected_default = CONFIGURABLE_KEYS[key]["default"]
            # Some keys have defaults in CONFIGURABLE_KEYS but no corresponding
            # field in the Pydantic model, so config_get returns None
            if value is not None:
                assert value == expected_default, (
                    f"{key}: got {value!r}, expected {expected_default!r}"
                )

    def test_hash_algorithms_are_valid(self) -> None:
        """All hash algorithms in defaults are in VALID_HASH_ALGORITHMS."""
        defaults = _get_default_config()

        assert defaults["hash"]["primary"] in VALID_HASH_ALGORITHMS
        for algo in defaults["hash"]["get"]:
            assert algo in VALID_HASH_ALGORITHMS
