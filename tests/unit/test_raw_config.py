from __future__ import annotations

from pathlib import Path

from roar.integrations.config.raw import (
    _derive_web_url_from_api,
    get_raw_glaas_web_url,
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
