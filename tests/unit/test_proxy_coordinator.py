"""Tests for RunCoordinator proxy integration and AWS_ENDPOINT_URL chaining."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.services.execution.coordinator import RunCoordinator
from roar.services.execution.proxy import ProxyHandle


def _make_ctx():
    """Create a minimal RunContext mock."""
    ctx = MagicMock()
    ctx.command = ["python", "train.py"]
    ctx.job_type = "run"
    ctx.repo_root = "/tmp/repo"
    ctx.roar_dir = Path("/tmp/repo/.roar")
    ctx.hash_algorithms = ["blake3"]
    return ctx


def _make_tracer_result():
    """Create a mock tracer result."""
    result = MagicMock()
    result.exit_code = 0
    result.duration = 1.0
    result.tracer_log_path = "/tmp/repo/.roar/run_1_tracer.msgpack"
    result.inject_log_path = "/tmp/repo/.roar/run_1_inject.json"
    result.interrupted = False
    return result


def _patch_coordinator_deps(coord):
    """Return a combined context manager that patches coordinator dependencies."""
    return (
        patch("os.path.exists", return_value=True),
        patch("roar.config.load_config", return_value={}),
    )


class TestProxyLifecycle:
    def _run_coord(self, coord):
        """Execute coordinator with all deps mocked."""
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("os.path.exists", return_value=True),
            patch("roar.config.load_config", return_value={}),
            patch("roar.services.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(coord, "_record_job", return_value=(1, "abc123", [], [], [], [])),
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            return coord.execute(_make_ctx())

    def test_start_for_run_called_when_proxy_service_provided(self):
        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        mock_proxy.start_for_run.assert_called_once()

    def test_stop_for_run_called_after_tracer(self):
        mock_proxy = MagicMock()
        handle = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.start_for_run.return_value = handle
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        mock_proxy.stop_for_run.assert_called_once_with(handle)

    def test_extra_env_with_aws_endpoint_url_passed_to_tracer(self):
        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert "extra_env" in call_kwargs
        assert call_kwargs["extra_env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9090"

    def test_proxy_start_failure_continues_without_proxy(self):
        mock_proxy = MagicMock()
        mock_proxy.start_for_run.side_effect = RuntimeError("binary not found")

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        result = self._run_coord(coord)

        # Execution should succeed despite proxy failure
        assert result.exit_code == 0
        # Tracer should have been called without extra_env
        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs.get("extra_env") is None

    def test_no_proxy_service_means_no_extra_env(self):
        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=None)
        self._run_coord(coord)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs.get("extra_env") is None


class TestEndpointUrlChaining:
    def _run_coord(self, coord):
        """Execute coordinator with all deps mocked."""
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("os.path.exists", return_value=True),
            patch("roar.config.load_config", return_value={}),
            patch("roar.services.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(coord, "_record_job", return_value=(1, "abc123", [], [], [], [])),
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            return coord.execute(_make_ctx())

    def test_existing_aws_endpoint_url_passed_as_upstream(self, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")

        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        mock_proxy.start_for_run.assert_called_once_with(upstream_url="http://localhost:4566")

    def test_no_aws_endpoint_url_means_upstream_none(self, monkeypatch):
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        mock_proxy.start_for_run.assert_called_once_with(upstream_url=None)

    def test_proxy_url_replaces_original_in_extra_env(self, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")

        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=8888)
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)
        self._run_coord(coord)

        # The child should see our proxy's URL, not the original
        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["extra_env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:8888"
