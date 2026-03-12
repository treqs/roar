"""Tests for RunCoordinator proxy integration and AWS_ENDPOINT_URL chaining."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.core.exceptions import TracerNotFoundError
from roar.services.execution.coordinator import RunCoordinator
from roar.services.execution.proxy import ProxyHandle


def _make_ctx():
    """Create a minimal RunContext mock."""
    ctx = MagicMock()
    ctx.command = ["python", "train.py"]
    ctx.execution_backend = "local"
    ctx.job_type = "run"
    ctx.repo_root = "/tmp/repo"
    ctx.roar_dir = Path("/tmp/repo/.roar")
    ctx.hash_algorithms = ["blake3"]
    ctx.tracer_mode = None
    ctx.tracer_fallback = None
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
        # Tracer still receives the selected execution backend
        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["extra_env"]["ROAR_EXECUTION_BACKEND"] == "local"
        assert "AWS_ENDPOINT_URL" not in call_kwargs["extra_env"]

    def test_no_proxy_service_means_no_extra_env(self):
        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=None)
        self._run_coord(coord)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["extra_env"] == {"ROAR_EXECUTION_BACKEND": "local"}

    def test_run_job_uid_is_forwarded_to_record_job(self):
        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=None)
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("secrets.token_hex", return_value="runuid12"),
            patch("os.path.exists", return_value=True),
            patch("roar.config.load_config", return_value={}),
            patch("roar.services.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(
                coord, "_record_job", return_value=(1, "abc123", [], [], [], [])
            ) as mock_record,
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            coord.execute(_make_ctx())

        assert mock_record.call_args.kwargs["run_job_uid"] == "runuid12"

    def test_tracer_overrides_are_forwarded(self):
        mock_proxy = MagicMock()
        mock_proxy.start_for_run.return_value = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.return_value = _make_tracer_result()

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)

        ctx = _make_ctx()
        ctx.tracer_mode = "ptrace"
        ctx.tracer_fallback = False

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
            coord.execute(ctx)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["tracer_mode_override"] == "ptrace"
        assert call_kwargs["fallback_enabled_override"] is False

    def test_proxy_is_stopped_on_tracer_not_found(self):
        mock_proxy = MagicMock()
        handle = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.start_for_run.return_value = handle
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        mock_tracer.execute.side_effect = TracerNotFoundError("no tracer")

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)

        with patch.object(coord, "_backup_previous_outputs"):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        mock_proxy.stop_for_run.assert_called_once_with(handle)

    def test_proxy_is_stopped_when_tracer_log_missing(self):
        mock_proxy = MagicMock()
        handle = ProxyHandle(process=MagicMock(), port=9090)
        mock_proxy.start_for_run.return_value = handle
        mock_proxy.stop_for_run.return_value = []

        mock_tracer = MagicMock()
        tracer_result = _make_tracer_result()
        tracer_result.exit_code = 1
        mock_tracer.execute.return_value = tracer_result

        coord = RunCoordinator(tracer_service=mock_tracer, proxy_service=mock_proxy)

        with (
            patch.object(coord, "_backup_previous_outputs"),
            patch("roar.config.load_config", return_value={}),
            patch("os.path.exists", return_value=False),
            patch.object(coord, "_cleanup_logs"),
        ):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        mock_proxy.stop_for_run.assert_called_once_with(handle)


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
