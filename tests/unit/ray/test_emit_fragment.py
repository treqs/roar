from __future__ import annotations

import pytest

from roar.ray.fragment import TaskFragment


def _build_fragment() -> TaskFragment:
    return TaskFragment(
        job_uid="job-123",
        parent_job_uid="parent-123",
        ray_task_id="task-123",
        ray_worker_id="worker-123",
        ray_node_id="node-123",
        ray_actor_id=None,
        function_name="run",
        started_at=1.0,
        ended_at=1.0,
        exit_code=0,
    )


def test_emit_fragment_initializes_streamer_and_appends_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_fragment_streamer", None)
    monkeypatch.setenv("ROAR_SESSION_ID", "session-abc")
    monkeypatch.setenv("ROAR_FRAGMENT_TOKEN", "ab" * 32)
    monkeypatch.setenv("GLAAS_URL", "https://api.dev.glaas.ai")
    monkeypatch.delenv("GLAAS_API_URL", raising=False)

    init_calls: list[tuple[str, str, str, int]] = []
    appended: list[dict] = []

    class _FakeStreamer:
        def __init__(
            self,
            session_id: str,
            token: str,
            glaas_url: str,
            flush_threshold: int = 50,
        ) -> None:
            init_calls.append((session_id, token, glaas_url, flush_threshold))

        def append_fragment(self, fragment_dict: dict) -> None:
            appended.append(fragment_dict)

        def close(self) -> None:
            return

    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _FakeStreamer)

    roar_worker._emit_fragment(_build_fragment())

    assert init_calls == [("session-abc", "ab" * 32, "https://api.dev.glaas.ai", 50)]
    assert appended
    assert appended[0]["job_uid"] == "job-123"


def test_emit_fragment_without_required_env_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_fragment_streamer", None)
    monkeypatch.delenv("ROAR_SESSION_ID", raising=False)
    monkeypatch.delenv("ROAR_FRAGMENT_TOKEN", raising=False)
    monkeypatch.delenv("GLAAS_URL", raising=False)
    monkeypatch.delenv("GLAAS_API_URL", raising=False)

    class _UnexpectedStreamer:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            raise AssertionError("streamer should not be created")

    monkeypatch.setattr(roar_worker, "GlaasFragmentStreamer", _UnexpectedStreamer)

    roar_worker._emit_fragment(_build_fragment())

    assert roar_worker._fragment_streamer is None


def test_atexit_handler_closes_streamer(monkeypatch: pytest.MonkeyPatch) -> None:
    import roar.ray.roar_worker as roar_worker

    monkeypatch.setattr(roar_worker, "_startup_complete", False)
    monkeypatch.setattr(roar_worker, "_fragment_streamer", None)
    monkeypatch.setattr(roar_worker, "_patch_boto3", lambda: None)
    monkeypatch.setattr(roar_worker, "_patch_pandas_parquet", lambda: None)
    monkeypatch.setattr(roar_worker, "_get_actor_attribution", lambda: "per_call")
    monkeypatch.setattr(roar_worker.builtins, "open", roar_worker._real_open)

    registered: list = []
    monkeypatch.setattr(roar_worker.atexit, "register", lambda fn: registered.append(fn))

    close_calls: list[str] = []

    class _FakeStreamer:
        def close(self) -> None:
            close_calls.append("close")

    monkeypatch.setattr(roar_worker, "_fragment_streamer", _FakeStreamer())

    roar_worker._startup()

    assert registered == [roar_worker._shutdown_streamer, roar_worker._flush_current_fragment]

    for handler in reversed(registered):
        handler()

    assert close_calls == ["close"]
