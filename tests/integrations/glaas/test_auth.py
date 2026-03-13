from roar.integrations.glaas import get_glaas_url


def test_get_glaas_url_prefers_env_over_config(monkeypatch):
    monkeypatch.setenv("GLAAS_URL", "https://env.example.com")
    monkeypatch.setattr(
        "roar.integrations.config.config_get", lambda key: "https://config.example.com"
    )

    assert get_glaas_url() == "https://env.example.com"


def test_get_glaas_url_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("GLAAS_URL", raising=False)
    monkeypatch.setattr(
        "roar.integrations.config.config_get", lambda key: "https://config.example.com"
    )

    assert get_glaas_url() == "https://config.example.com"
