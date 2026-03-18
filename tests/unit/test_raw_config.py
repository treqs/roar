from __future__ import annotations

from pathlib import Path

from roar.integrations.config import config_get
from roar.integrations.config.raw import (
    get_raw_glaas_web_url,
    get_raw_registration_omit_config,
)


def test_raw_registration_omit_matches_full_config_defaults(tmp_path: Path) -> None:
    assert get_raw_registration_omit_config(start_dir=str(tmp_path)) == config_get(
        "registration.omit",
        start_dir=str(tmp_path),
    )


def test_raw_registration_omit_matches_file_and_env_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        """
[registration.omit]
enabled = false

[registration.omit.secrets]
values = ["literal-secret"]

[registration.omit.allowlist]
patterns = ["safe-.*"]
""".strip()
    )
    monkeypatch.setenv(
        "ROAR_REGISTRATION__OMIT__ENV_VARS__NAMES",
        '["LOCAL_API_TOKEN", "SECOND_TOKEN"]',
    )

    assert get_raw_registration_omit_config(start_dir=str(tmp_path)) == config_get(
        "registration.omit",
        start_dir=str(tmp_path),
    )


def test_raw_glaas_web_url_prefers_env_over_file(tmp_path: Path, monkeypatch) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        """
[glaas]
web_url = "https://glaas.example/app/"
""".strip()
    )

    assert get_raw_glaas_web_url(start_dir=str(tmp_path)) == "https://glaas.example/app"

    monkeypatch.setenv("ROAR_GLAAS__WEB_URL", "https://override.example/ui/")
    assert get_raw_glaas_web_url(start_dir=str(tmp_path)) == "https://override.example/ui"
