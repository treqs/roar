from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from roar.ray import collector


class _FakeRemoteMethod:
    def __init__(self, value):
        self._value = value

    def remote(self):
        return self._value


class _FakeActor:
    def __init__(self, events):
        self.get_all = _FakeRemoteMethod(events)
        self.flush_to_glaas = _FakeRemoteMethod(True)


class _FakeRayWithActor:
    def __init__(self, events):
        self.actor = _FakeActor(events)
        self.get_actor_calls: list[tuple[str, str | None]] = []
        self.get_calls: list[tuple[object, int | None]] = []
        self.killed = False

    def is_initialized(self) -> bool:
        return True

    def get_actor(self, name: str, namespace: str | None = None):
        self.get_actor_calls.append((name, namespace))
        return self.actor

    def get(self, value, timeout: int | None = None):
        self.get_calls.append((value, timeout))
        return value

    def kill(self, actor) -> None:
        if actor is self.actor:
            self.killed = True


class _FakeRayNoActor:
    def is_initialized(self) -> bool:
        return True

    def get_actor(self, _name: str, namespace: str | None = None):
        del namespace
        raise ValueError("actor not found")


def test_collect_events_prefers_actor_when_ray_is_initialized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fallback_log = tmp_path / "task-fallback.jsonl"
    fallback_log.write_text('{"task_id":"task-fallback","path":"/tmp/fs.txt","mode":"w"}\n')

    fake_ray = _FakeRayWithActor([{"task_id": "task-actor", "path": "/tmp/actor.txt", "mode": "w"}])
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setenv("ROAR_JOB_ID", "job1234")

    events = collector._collect_events(tmp_path)

    assert set(events) == {"task-actor"}
    assert events["task-actor"][0]["path"] == "/tmp/actor.txt"
    assert fake_ray.get_actor_calls == [("roar-log-collector-job1234", "roar")]
    assert fake_ray.get_calls == [
        ([{"task_id": "task-actor", "path": "/tmp/actor.txt", "mode": "w"}], 30),
        (True, 5),
    ]
    assert fake_ray.killed is True


def test_collect_events_falls_back_to_filesystem_when_actor_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "task-fs.jsonl"
    log_file.write_text(
        json.dumps({"task_id": "task-fs", "path": "/tmp/fs.txt", "mode": "r"}) + "\n"
    )

    monkeypatch.setitem(sys.modules, "ray", _FakeRayNoActor())

    events = collector._collect_events(tmp_path)

    assert set(events) == {"task-fs"}
    assert events["task-fs"][0]["path"] == "/tmp/fs.txt"
