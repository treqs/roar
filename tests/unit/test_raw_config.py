from __future__ import annotations

from pathlib import Path

from roar.integrations.config import config_get
from roar.integrations.config.raw import (
    _derive_web_url_from_api,
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


def test_derive_web_url_from_api_strips_api_label() -> None:
    assert _derive_web_url_from_api("https://api.glaas.ai") == "https://glaas.ai"
    assert _derive_web_url_from_api("https://api.dev.glaas.ai") == "https://dev.glaas.ai"
    assert _derive_web_url_from_api("https://api.glaas.ai/api/v1") == "https://glaas.ai"


def test_derive_web_url_from_api_no_api_prefix_returns_none() -> None:
    # Hosts without an api. prefix can't be transformed reliably.
    assert _derive_web_url_from_api("http://localhost:3000") is None
    assert _derive_web_url_from_api(None) is None


def test_raw_glaas_web_url_derives_from_api_url_when_web_url_unset(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ROAR_GLAAS__WEB_URL", raising=False)
    monkeypatch.delenv("ROAR_GLAAS__URL", raising=False)
    monkeypatch.delenv("GLAAS_URL", raising=False)
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    # Only the API URL is configured (the reported scenario) — the web link
    # must follow the API to dev, not fall back to prod.
    (roar_dir / "config.toml").write_text(
        """
[glaas]
url = "https://api.dev.glaas.ai"
""".strip()
    )

    assert get_raw_glaas_web_url(start_dir=str(tmp_path)) == "https://dev.glaas.ai"
