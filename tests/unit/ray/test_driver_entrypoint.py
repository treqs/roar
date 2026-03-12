from __future__ import annotations

import types

from roar.backends.ray.plugin import ray_build_driver_proxy_fragment
from roar.execution.framework.contract import (
    DistributedExecutionBackend,
    DriverBootstrapAdapter,
    WorkerBootstrapAdapter,
)
from roar.services.execution import driver_entrypoint
from roar.services.execution.proxy import S3LogEntry


def test_build_driver_proxy_fragment_maps_s3_entries_to_reads_and_writes(monkeypatch) -> None:
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")

    fragment = ray_build_driver_proxy_fragment(
        [
            S3LogEntry(
                operation="PutObject",
                bucket="bucket",
                key="output.json",
                etag="etag-out",
                size_bytes=12,
            ),
            S3LogEntry(
                operation="GetObject",
                bucket="bucket",
                key="input.json",
                etag="etag-in",
                size_bytes=34,
            ),
        ],
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
        environ=driver_entrypoint.os.environ,
    )

    assert fragment is not None
    assert fragment["parent_job_uid"] == "job-123"
    assert fragment["function_name"] == "s3_driver_proxy"
    assert [ref["path"] for ref in fragment["writes"]] == ["s3://bucket/output.json"]
    assert [ref["hash"] for ref in fragment["writes"]] == ["etag-out"]
    assert [ref["path"] for ref in fragment["reads"]] == ["s3://bucket/input.json"]
    assert [ref["hash"] for ref in fragment["reads"]] == ["etag-in"]


def test_emit_driver_proxy_fragment_streams_to_glaas_when_session_is_present(
    monkeypatch,
) -> None:
    fragment = {"job_uid": "task-1"}

    captured: dict[str, object] = {}

    def _fake_emit_fragment_dicts(fragments, *, env, local_merge):
        captured["fragments"] = list(fragments)
        captured["env"] = env
        captured["local_merge"] = local_merge
        return "streamed"

    monkeypatch.setattr(driver_entrypoint, "emit_fragment_dicts", _fake_emit_fragment_dicts)
    monkeypatch.setattr(
        driver_entrypoint,
        "get_execution_backend",
        lambda _name: DistributedExecutionBackend(
            name="ray",
            matches_submit_command=lambda _command: False,
            rewrite_submit_command=lambda command: None,
            driver_bootstrap=DriverBootstrapAdapter(
                build_proxy_fragment=lambda *_args, **_kwargs: None,
                local_merge=lambda *_args, **_kwargs: None,
            ),
            worker_bootstrap=WorkerBootstrapAdapter(
                py_executable="roar-worker",
                setup_hook="roar.services.execution.worker_bootstrap.startup",
                prepare_runtime_env=lambda runtime_env, _job_id, _env: dict(runtime_env or {}),
                startup=lambda: None,
                run_entrypoint=lambda _argv: None,
            ),
        ),
    )

    driver_entrypoint._emit_driver_proxy_fragment(fragment, environ=driver_entrypoint.os.environ)

    assert captured["fragments"] == [fragment]
    assert captured["env"] is driver_entrypoint.os.environ
    assert callable(captured["local_merge"])


def test_start_driver_proxy_uses_configured_local_port(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeProxyService:
        def start_for_run(
            self,
            *,
            session_id: str | None = None,
            job_id: str | None = None,
            upstream_url: str | None = None,
            port: int | None = None,
        ):
            calls.append(
                {
                    "session_id": session_id,
                    "job_id": job_id,
                    "upstream_url": upstream_url,
                    "port": port,
                }
            )
            return types.SimpleNamespace(port=port)

    monkeypatch.setattr(driver_entrypoint, "ProxyService", _FakeProxyService)
    monkeypatch.setattr(driver_entrypoint, "_should_start_driver_proxy", lambda _environ: True)
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:24567")
    monkeypatch.setenv("ROAR_PROXY_PORT", "24567")
    monkeypatch.delenv("ROAR_UPSTREAM_S3_ENDPOINT", raising=False)

    _service, handle = driver_entrypoint._start_driver_proxy(driver_entrypoint.os.environ)

    assert handle is not None
    assert calls == [
        {
            "session_id": None,
            "job_id": "job-123",
            "upstream_url": None,
            "port": 24567,
        }
    ]


def test_main_preserves_loopback_proxy_endpoint_for_child_process(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:24567")
    monkeypatch.setenv("ROAR_PROXY_PORT", "24567")

    class _FakeService:
        def stop_for_run(self, handle) -> list[S3LogEntry]:
            return []

    def _fake_start_driver_proxy():
        return _FakeService(), types.SimpleNamespace(port=24567)

    def _fake_run_child(argv, env):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        return 0

    monkeypatch.setattr(
        driver_entrypoint,
        "_start_driver_proxy",
        lambda _environ: _fake_start_driver_proxy(),
    )
    monkeypatch.setattr(driver_entrypoint, "_run_child", _fake_run_child)
    monkeypatch.setattr(
        driver_entrypoint,
        "_build_driver_proxy_fragment",
        lambda *_args, **_kwargs: None,
    )

    exit_code = driver_entrypoint.main(["python", "main.py"])

    assert exit_code == 0
    assert captured["argv"] == ["python", "main.py"]
    env = captured["env"]
    assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:24567"
    assert env["ROAR_PROXY_PORT"] == "24567"
