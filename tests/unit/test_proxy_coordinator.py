"""Tests for RunCoordinator runtime-resource integration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.core.exceptions import TracerNotFoundError, TracerPreflightError
from roar.execution.cluster.proxy import S3LogEntry
from roar.execution.runtime.coordinator import RunCoordinator
from roar.execution.runtime.resources import RuntimeObservationBundle, RuntimeResourceStart


class _FakeRuntimeResource:
    name = "proxy"

    def __init__(
        self,
        *,
        start_env: dict[str, str] | None = None,
        stop_result: RuntimeObservationBundle | None = None,
    ) -> None:
        self.start_env = start_env or {}
        self.stop_result = stop_result or RuntimeObservationBundle()
        self.start_calls: list[dict[str, object]] = []
        self.stop_calls: list[int | None] = []
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, ctx, environ):
        self.start_calls.append({"ctx": ctx, "environ": dict(environ)})
        self._active = bool(self.start_env)
        return RuntimeResourceStart(env=self.start_env)

    def stop(self, *, exit_code: int | None) -> RuntimeObservationBundle:
        self.stop_calls.append(exit_code)
        self._active = False
        return self.stop_result


def _make_ctx():
    ctx = MagicMock()
    ctx.command = ["python", "train.py"]
    ctx.execution_backend = "local"
    ctx.job_type = "run"
    ctx.repo_root = "/tmp/repo"
    ctx.roar_dir = Path("/tmp/repo/.roar")
    ctx.hash_algorithms = ["blake3"]
    ctx.tracer_mode = None
    ctx.tracer_fallback = None
    ctx.quiet = False
    return ctx


def _make_tracer_result():
    result = MagicMock()
    result.exit_code = 0
    result.duration = 1.0
    result.tracer_log_path = "/tmp/repo/.roar/run_1_tracer.msgpack"
    result.inject_log_path = "/tmp/repo/.roar/run_1_inject.json"
    result.interrupted = False
    result.backend = "ptrace"
    return result


def _make_mock_tracer():
    tracer = MagicMock()
    tracer.resolve_execution_candidates.return_value = [("ptrace", "/fake/roar-tracer")]
    tracer.execute.return_value = _make_tracer_result()
    return tracer


class TestRuntimeResourceLifecycle:
    def _run_coord(self, coord, ctx=None):
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("os.path.exists", return_value=True),
            patch("roar.integrations.config.load_config", return_value={}),
            patch("roar.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(coord, "_record_job", return_value=(1, "abc123", [], [], [], [], {})),
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            return coord.execute(ctx or _make_ctx())

    def test_runtime_resource_start_called_when_configured(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        self._run_coord(coord)

        assert len(resource.start_calls) == 1

    def test_runtime_resource_stop_called_after_tracer(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        self._run_coord(coord)

        assert resource.stop_calls == [0]

    def test_runtime_resource_env_patch_is_forwarded_to_tracer(self):
        resource = _FakeRuntimeResource(
            start_env={
                "AWS_ENDPOINT_URL": "http://127.0.0.1:9090",
                "ROAR_UPSTREAM_S3_ENDPOINT": "http://localhost:4566",
            }
        )
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        self._run_coord(coord)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["extra_env"]["ROAR_EXECUTION_BACKEND"] == "local"
        assert call_kwargs["extra_env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9090"
        assert call_kwargs["extra_env"]["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://localhost:4566"

    def test_no_runtime_resource_means_no_extra_env_beyond_backend(self):
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[])
        self._run_coord(coord)

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["extra_env"] == {"ROAR_EXECUTION_BACKEND": "local"}

    def test_run_job_uid_is_forwarded_to_record_job(self):
        mock_tracer = _make_mock_tracer()
        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[])
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("secrets.token_hex", return_value="runuid12"),
            patch("os.path.exists", return_value=True),
            patch("roar.integrations.config.load_config", return_value={}),
            patch("roar.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(
                coord, "_record_job", return_value=(1, "abc123", [], [], [], [], {})
            ) as mock_record,
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            coord.execute(_make_ctx())

        assert mock_record.call_args.kwargs["run_job_uid"] == "runuid12"

    def test_tracer_overrides_are_forwarded(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        ctx = _make_ctx()
        ctx.tracer_mode = "ptrace"
        ctx.tracer_fallback = False
        self._run_coord(coord, ctx=ctx)

        resolve_kwargs = mock_tracer.resolve_execution_candidates.call_args.kwargs
        assert resolve_kwargs["tracer_mode_override"] == "ptrace"
        assert resolve_kwargs["fallback_enabled_override"] is False

        call_kwargs = mock_tracer.execute.call_args.kwargs
        assert call_kwargs["tracer_mode_override"] == "ptrace"
        assert call_kwargs["fallback_enabled_override"] is False
        assert call_kwargs["candidates_override"] == [("ptrace", "/fake/roar-tracer")]

    def test_runtime_resource_is_stopped_on_tracer_not_found(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()
        mock_tracer.resolve_execution_candidates.side_effect = TracerNotFoundError("no tracer")

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        with patch.object(coord, "_backup_previous_outputs"):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        assert resource.stop_calls == [1]

    def test_runtime_resource_is_stopped_on_tracer_preflight_failure(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()
        mock_tracer.resolve_execution_candidates.side_effect = TracerPreflightError(
            "preflight failed"
        )

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        with patch.object(coord, "_backup_previous_outputs"):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        assert resource.stop_calls == [1]

    def test_tracer_preflight_error_prints_human_message_without_context_dump(self):
        mock_tracer = _make_mock_tracer()
        mock_tracer.resolve_execution_candidates.side_effect = TracerPreflightError(
            "preflight failed\n\nNext steps:\n  - try another backend",
            context={"failures": [{"backend": "ebpf", "summary": "debug-only detail"}]},
        )
        presenter = MagicMock()

        coord = RunCoordinator(
            tracer_service=mock_tracer,
            runtime_resources=[],
            presenter=presenter,
        )
        with patch.object(coord, "_backup_previous_outputs"):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        presenter.print_error.assert_called_once()
        printed = presenter.print_error.call_args.args[0]
        assert "preflight failed" in printed
        assert "Next steps:" in printed
        assert "failures=" not in printed
        assert "debug-only detail" not in printed

    def test_runtime_resource_is_stopped_when_tracer_log_missing(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()
        tracer_result = _make_tracer_result()
        tracer_result.exit_code = 1
        mock_tracer.execute.return_value = tracer_result

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])

        with (
            patch.object(coord, "_backup_previous_outputs"),
            patch("roar.integrations.config.load_config", return_value={}),
            patch("os.path.exists", return_value=False),
            patch.object(coord, "_cleanup_logs"),
        ):
            result = coord.execute(_make_ctx())

        assert result.exit_code == 1
        assert resource.stop_calls == [1]

    def test_runtime_resource_observations_are_forwarded_to_record_job(self):
        s3_entries = (
            S3LogEntry(operation="GetObject", bucket="bucket", key="input.csv", etag="etag"),
        )
        resource = _FakeRuntimeResource(
            start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"},
            stop_result=RuntimeObservationBundle(s3_entries=s3_entries),
        )
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        mock_prov = MagicMock()
        mock_prov.collect.return_value = {"data": {"read_files": [], "written_files": []}}

        with (
            patch("os.path.exists", return_value=True),
            patch("roar.integrations.config.load_config", return_value={}),
            patch("roar.execution.provenance.ProvenanceService", return_value=mock_prov),
            patch.object(
                coord, "_record_job", return_value=(1, "abc123", [], [], [], [], {})
            ) as mock_record,
            patch.object(coord, "_backup_previous_outputs"),
            patch.object(coord, "_cleanup_logs"),
        ):
            coord.execute(_make_ctx())

        assert mock_record.call_args.args[5] == list(s3_entries)

    def test_proxy_active_is_reflected_in_run_result(self):
        resource = _FakeRuntimeResource(start_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"})
        mock_tracer = _make_mock_tracer()

        coord = RunCoordinator(tracer_service=mock_tracer, runtime_resources=[resource])
        result = self._run_coord(coord)

        assert result.proxy_active is True
