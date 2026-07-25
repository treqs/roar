from unittest.mock import MagicMock

import pytest

from roar.execution.cluster.proxy import ProxyHandle, S3LogEntry
from roar.execution.runtime.errors import ExecutionSetupError
from roar.execution.runtime.proxy_resource import ProxyRuntimeResource


@pytest.fixture
def ctx():
    value = MagicMock()
    value.repo_root = "/tmp/repo"
    value.roar_dir = "/tmp/repo/.roar"
    return value


def test_init_raises_when_proxy_binary_is_missing():
    service = MagicMock()
    service.find_proxy.return_value = None

    with pytest.raises(ExecutionSetupError, match="roar-proxy binary not found"):
        ProxyRuntimeResource(service=service)


def test_start_passes_existing_s3_endpoint_as_upstream(ctx):
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    service.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)

    resource = ProxyRuntimeResource(service=service)
    result = resource.start(ctx, {"AWS_ENDPOINT_URL_S3": "http://localhost:4566"})

    service.start_for_run.assert_called_once_with(upstream_url="http://localhost:4566")
    assert result.env == {
        "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9090",
        "S3_ENDPOINT_URL": "http://127.0.0.1:9090",
        "ROAR_UPSTREAM_S3_ENDPOINT": "http://localhost:4566",
    }
    assert resource.active is True


def test_start_detects_s5cmd_endpoint_var_as_upstream(ctx):
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    service.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)

    resource = ProxyRuntimeResource(service=service)
    result = resource.start(ctx, {"S3_ENDPOINT_URL": "http://localhost:4566"})

    service.start_for_run.assert_called_once_with(upstream_url="http://localhost:4566")
    assert result.env["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://localhost:4566"


def test_start_detects_generic_aws_endpoint_url_as_upstream(ctx):
    """The generic AWS_ENDPOINT_URL (the common MinIO/LocalStack setup) is
    detected as the upstream to chain to, even though we inject the S3-scoped
    vars. Regression guard for the case where a user only set AWS_ENDPOINT_URL."""
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    service.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)

    resource = ProxyRuntimeResource(service=service)
    result = resource.start(ctx, {"AWS_ENDPOINT_URL": "http://localhost:4566"})

    service.start_for_run.assert_called_once_with(upstream_url="http://localhost:4566")
    assert result.env["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://localhost:4566"
    # still redirects S3 clients via the scoped vars, not the generic one
    assert result.env["AWS_ENDPOINT_URL_S3"] == "http://127.0.0.1:9090"
    assert "AWS_ENDPOINT_URL" not in result.env


def test_start_without_existing_endpoint_only_sets_proxy_url(ctx):
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    service.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)

    resource = ProxyRuntimeResource(service=service)
    result = resource.start(ctx, {})

    service.start_for_run.assert_called_once_with(upstream_url=None)
    assert result.env == {
        "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9090",
        "S3_ENDPOINT_URL": "http://127.0.0.1:9090",
    }


def test_start_failure_returns_empty_env_and_keeps_resource_inactive(ctx):
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    service.start_for_run.side_effect = RuntimeError("boom")

    resource = ProxyRuntimeResource(service=service)
    result = resource.start(ctx, {})

    assert result.env == {}
    assert resource.active is False


def test_stop_returns_collected_s3_entries_and_deactivates(ctx):
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"
    handle = ProxyHandle(process=MagicMock(), port=9090)
    service.start_for_run.return_value = handle
    entries = [S3LogEntry(operation="GetObject", bucket="bucket", key="input.csv", etag="abc")]
    service.stop_for_run.return_value = entries

    resource = ProxyRuntimeResource(service=service)
    resource.start(ctx, {})

    result = resource.stop(exit_code=0)

    service.stop_for_run.assert_called_once_with(handle)
    assert result.s3_entries == tuple(entries)
    assert resource.active is False


def test_stop_without_active_handle_returns_empty_observations():
    service = MagicMock()
    service.find_proxy.return_value = "/tmp/roar-proxy"

    resource = ProxyRuntimeResource(service=service)
    result = resource.stop(exit_code=0)

    assert result.s3_entries == ()
    service.stop_for_run.assert_not_called()
