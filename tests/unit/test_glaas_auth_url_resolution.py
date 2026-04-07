from __future__ import annotations

import pytest

from roar.glaas_auth import GlaasAuthError, resolve_auth_api_url


def test_resolve_auth_api_url_prefers_glaas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLAAS_API_URL", "https://api.glaas.example")
    monkeypatch.setenv("TREQS_API_URL", "https://api.treqs.example")

    assert resolve_auth_api_url() == "https://api.glaas.example"


def test_resolve_auth_api_url_ignores_treqs_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLAAS_API_URL", raising=False)
    monkeypatch.delenv("ROAR_GLAAS__URL", raising=False)
    monkeypatch.setenv("TREQS_API_URL", "https://api.treqs.example")

    assert resolve_auth_api_url() == "https://api.glaas.ai"


def test_resolve_auth_api_url_allows_localhost_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLAAS_API_URL", raising=False)
    monkeypatch.delenv("TREQS_API_URL", raising=False)
    assert resolve_auth_api_url("http://localhost:3001") == "http://localhost:3001"
    assert resolve_auth_api_url("http://127.0.0.1:3001") == "http://127.0.0.1:3001"


def test_resolve_auth_api_url_rejects_non_local_http_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROAR_ALLOW_HTTP", raising=False)

    with pytest.raises(GlaasAuthError, match="non-local insecure HTTP GLaaS API URL"):
        resolve_auth_api_url("http://example.com")
