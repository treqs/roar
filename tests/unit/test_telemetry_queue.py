from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from roar.telemetry import paths, payload, queue, stats, uploader


def _paths(tmp_path: Path) -> paths.TelemetryPaths:
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    return paths.resolve_paths(env)


def _payload(event_id: str) -> dict[str, Any]:
    return payload.build_payload(
        "init",
        {
            "stats_schema_version": 1,
            "install_id": "install-1",
            "sequence": 1,
            "first_seen": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-02T00:00:00Z",
            "subcommand_counts": {"init": 1},
            "tracer_selections": {},
        },
        event_id=event_id,
        trigger_ts="2026-05-03T00:00:00Z",
        version="1.2.3",
        platform_info={
            "os": "linux",
            "arch": "x86_64",
            "python": "3.12",
            "shell": "bash",
            "installer": "unknown",
            "containerized": False,
        },
        feature_capabilities={
            "glaas_endpoint_kind": "custom",
            "proxy_enabled": False,
            "ray_enabled": False,
            "osmo_enabled": False,
        },
    )


def test_queue_enqueue_is_idempotent_by_event_id(tmp_path: Path) -> None:
    resolved_paths = _paths(tmp_path)
    event = _payload("event-1")

    first = queue.enqueue_payload(event, paths=resolved_paths)
    second = queue.enqueue_payload(event, paths=resolved_paths)

    assert first.enqueued
    assert first.path is not None
    assert not second.enqueued
    assert second.reason == "duplicate_event_id"
    assert queue.queued_event_count(resolved_paths) == 1
    assert json.loads(first.path.read_text(encoding="utf-8"))["event_id"] == "event-1"


def test_queue_retention_removes_expired_events(tmp_path: Path) -> None:
    resolved_paths = _paths(tmp_path)
    result = queue.enqueue_payload(_payload("event-old"), paths=resolved_paths)
    assert result.path is not None
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(result.path, (old, old))

    removed = queue.cleanup_expired(paths=resolved_paths, retention_days=7)

    assert removed == 1
    assert queue.queued_event_count(resolved_paths) == 0


def test_uploader_deletes_event_only_after_204(tmp_path: Path, monkeypatch) -> None:
    resolved_paths = _paths(tmp_path)
    queue.enqueue_payload(_payload("event-204"), paths=resolved_paths)
    captured: dict[str, Any] = {}

    class AcceptedResponse:
        status = 204

        def __enter__(self) -> AcceptedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: Any, timeout: int) -> AcceptedResponse:
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return AcceptedResponse()

    monkeypatch.setattr(uploader.urllib.request, "urlopen", fake_urlopen)

    summary = uploader.drain_queue(
        environ={"ROAR_TELEMETRY_ENDPOINT": "https://collector.example/roar"},
        paths=resolved_paths,
    )

    assert summary.attempted == 1
    assert summary.accepted == 1
    assert summary.retained == 0
    assert queue.queued_event_count(resolved_paths) == 0
    assert captured["url"] == "https://collector.example/roar"
    assert captured["timeout"] == uploader.TOTAL_TIMEOUT_SECONDS
    assert json.loads(captured["data"].decode("utf-8"))["event_id"] == "event-204"


def test_uploader_network_post_does_not_hold_telemetry_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resolved_paths = _paths(tmp_path)
    queue.enqueue_payload(_payload("event-slow"), paths=resolved_paths)
    post_started = threading.Event()
    release_post = threading.Event()

    def slow_post(_endpoint: str, _payload: dict[str, Any]) -> bool:
        post_started.set()
        assert release_post.wait(timeout=5)
        return False

    monkeypatch.setattr(uploader, "_post_payload", slow_post)
    upload_thread = threading.Thread(
        target=uploader.drain_queue,
        kwargs={
            "environ": {"ROAR_TELEMETRY_ENDPOINT": "https://collector.example/roar"},
            "paths": resolved_paths,
        },
    )
    upload_thread.start()
    assert post_started.wait(timeout=5)

    started_at = time.monotonic()
    result = stats.record_subcommand("status", paths=resolved_paths, version="1.2.3")
    elapsed = time.monotonic() - started_at

    release_post.set()
    upload_thread.join(timeout=5)

    assert not upload_thread.is_alive()
    assert result.recorded
    assert elapsed < 0.5
