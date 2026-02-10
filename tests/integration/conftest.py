"""Integration test fixtures — re-export shared fixtures."""

import sys

import pytest

from .fake_s3 import FakeS3Server


@pytest.fixture
def python_exe() -> str:
    """Return the absolute path to the Python executable."""
    return sys.executable


@pytest.fixture(scope="session")
def fake_s3():
    """Session-scoped fake S3 server for proxy integration tests."""
    with FakeS3Server() as server:
        yield server


@pytest.fixture(scope="session")
def proxy_binary():
    """Locate the roar-proxy binary, skip if not found."""
    from roar.services.execution.proxy import ProxyService

    path = ProxyService().find_proxy()
    if path is None:
        pytest.skip("roar-proxy binary not found — build with: cd proxy && cargo build")
    return path


@pytest.fixture
def proxy_repo(temp_git_repo, fake_s3, proxy_binary, git_commit, monkeypatch):
    """Temp git repo with proxy enabled and AWS env pointing at fake S3.

    Enables proxy in config, sets AWS credentials and endpoint URL
    so that the coordinator chains traffic through the fake S3 server.
    """
    config_path = temp_git_repo / ".roar" / "config.toml"
    config_text = config_path.read_text()
    config_text += "\n[proxy]\nenabled = true\n"
    config_path.write_text(config_text)

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", f"http://127.0.0.1:{fake_s3.port}")

    git_commit("enable proxy")
    return temp_git_repo
