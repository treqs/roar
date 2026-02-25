from __future__ import annotations

from pathlib import Path

from roar.ray import collector


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def warning(self, message: str, *args) -> None:
        if args:
            message = message % args
        self.warning_messages.append(message)


def test_read_events_skips_corrupt_and_unreadable_jsonl_with_warnings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "task-good.jsonl").write_text(
        '{"path": "/shared/in.csv", "mode": "r", "task_id": "task-good"}\n'
        '{"not": "json"\n'
    )
    (tmp_path / "task-bad.jsonl").mkdir()

    fake_logger = _FakeLogger()
    monkeypatch.setattr(collector, "_get_logger", lambda: fake_logger)

    events = collector._read_events(tmp_path)

    assert "task-good" in events
    assert len(events["task-good"]) == 1
    assert fake_logger.warning_messages
    assert any("task-bad.jsonl" in msg for msg in fake_logger.warning_messages)
    assert any("task-good.jsonl" in msg for msg in fake_logger.warning_messages)
