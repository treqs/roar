from __future__ import annotations

from roar.ray import collector


def test_merge_proxy_logs_parses_s3_lines_into_events() -> None:
    task_events: dict[str, list[dict[str, object]]] = {}
    proxy_logs = {
        "node-abc": {
            "node_id": "node-abc",
            "proxy_log_lines": [
                "ROAR_PROXY_READY port=12345",
                "[S3:GetObject] s3://bucket/input.csv  etag=etag-in",
                "[S3:PutObject] s3://bucket/output.csv  etag=etag-out",
            ],
        }
    }

    collector._merge_proxy_logs(task_events, proxy_logs)

    assert set(task_events) == {"proxy-node-abc"}
    events = task_events["proxy-node-abc"]
    assert len(events) == 2

    read_event, write_event = events
    assert read_event["path"] == "s3://bucket/input.csv"
    assert read_event["mode"] == "r"
    assert read_event["capture_method"] == "proxy"
    assert read_event["hash"] == "etag-in"

    assert write_event["path"] == "s3://bucket/output.csv"
    assert write_event["mode"] == "w"
    assert write_event["capture_method"] == "proxy"
    assert write_event["hash"] == "etag-out"
