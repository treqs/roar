from __future__ import annotations

import types

from roar.ray import driver_entrypoint
from roar.services.execution.proxy import S3LogEntry


def test_build_driver_proxy_fragment_maps_s3_entries_to_reads_and_writes(monkeypatch) -> None:
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")

    fragment = driver_entrypoint._build_driver_proxy_fragment(
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
    )

    assert fragment is not None
    assert fragment.parent_job_uid == "job-123"
    assert fragment.function_name == "s3_driver_proxy"
    assert [ref.path for ref in fragment.writes] == ["s3://bucket/output.json"]
    assert [ref.hash for ref in fragment.writes] == ["etag-out"]
    assert [ref.path for ref in fragment.reads] == ["s3://bucket/input.json"]
    assert [ref.hash for ref in fragment.reads] == ["etag-in"]


def test_emit_driver_proxy_fragment_streams_to_glaas_when_session_is_present(
    monkeypatch,
) -> None:
    fragment = driver_entrypoint.TaskFragment(
        job_uid="task-1",
        parent_job_uid="job-1",
        ray_task_id="proxy:driver",
        ray_worker_id="",
        ray_node_id="driver",
        ray_actor_id=None,
        function_name="s3_driver_proxy",
        started_at=1.0,
        ended_at=2.0,
        exit_code=0,
    )

    calls: list[tuple[str, object]] = []

    class _FakeStreamer:
        def __init__(self, *, session_id: str, token: str, glaas_url: str) -> None:
            calls.append(("init", (session_id, token, glaas_url)))

        def append_fragment(self, payload: dict[str, object]) -> None:
            calls.append(("append", payload))

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(driver_entrypoint, "GlaasFragmentStreamer", _FakeStreamer)
    monkeypatch.setattr(driver_entrypoint, "collect_fragments", lambda *args, **kwargs: calls.append(("collect", args or kwargs)))
    monkeypatch.setenv("ROAR_SESSION_ID", "session-1")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ab" * 32)
    monkeypatch.setenv("GLAAS_URL", "http://localhost:3001")

    driver_entrypoint._emit_driver_proxy_fragment(fragment)

    assert calls[0] == ("init", ("session-1", "ab" * 32, "http://localhost:3001"))
    assert calls[1][0] == "append"
    assert calls[2] == ("close", None)
    assert all(kind != "collect" for kind, _payload in calls)


def test_start_driver_proxy_uses_fixed_local_port(monkeypatch) -> None:
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
    monkeypatch.setenv("ROAR_JOB_ID", "job-123")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:19191")
    monkeypatch.delenv("ROAR_UPSTREAM_S3_ENDPOINT", raising=False)

    _service, handle = driver_entrypoint._start_driver_proxy()

    assert handle is not None
    assert calls == [
        {
            "session_id": None,
            "job_id": "job-123",
            "upstream_url": None,
            "port": 19191,
        }
    ]


def test_main_preserves_loopback_proxy_endpoint_for_child_process(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:19191")

    class _FakeService:
        def stop_for_run(self, handle) -> list[S3LogEntry]:
            return []

    def _fake_start_driver_proxy():
        return _FakeService(), types.SimpleNamespace(port=19191)

    def _fake_run_child(argv, env):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        return 0

    monkeypatch.setattr(driver_entrypoint, "_start_driver_proxy", _fake_start_driver_proxy)
    monkeypatch.setattr(driver_entrypoint, "_run_child", _fake_run_child)

    exit_code = driver_entrypoint.main(["python", "main.py"])

    assert exit_code == 0
    assert captured["argv"] == ["python", "main.py"]
    env = captured["env"]
    assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:19191"
    assert env["ROAR_PROXY_PORT"] == "19191"
