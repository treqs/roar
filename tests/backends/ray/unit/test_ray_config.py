from __future__ import annotations

from roar.config import load_config


def test_load_config_includes_ray_defaults(tmp_path) -> None:
    config = load_config(start_dir=str(tmp_path))

    assert config["ray"]["enabled"] is True
    assert config["ray"]["pip_install"] is True
    assert config["ray"]["actor_attribution"] == "per_call"


def test_load_config_reads_ray_section(tmp_path) -> None:
    config_path = tmp_path / ".roar" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("""
[ray]
enabled = false
pip_install = false
actor_attribution = "per_actor"
""")

    config = load_config(start_dir=str(tmp_path))

    assert config["ray"]["enabled"] is False
    assert config["ray"]["pip_install"] is False
    assert config["ray"]["actor_attribution"] == "per_actor"
