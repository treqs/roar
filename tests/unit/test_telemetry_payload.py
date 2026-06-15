from __future__ import annotations

import json

from roar.telemetry import payload


def test_payload_builder_only_serializes_allowlisted_fields() -> None:
    stats_snapshot = {
        "stats_schema_version": 1,
        "install_id": "install-1",
        "sequence": 7,
        "first_seen": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
        "command": "python train.py --secret customer",
        "path": "/private/customer/repo",
        "environment": {"API_KEY": "secret"},
        "subcommand_counts": {
            "run": 3,
            "init": 1,
            "secret_command": 99,
        },
        "tracer_selections": {
            "ptrace": 2,
            "/private/libroar.so": 1,
        },
        "feature_capabilities": {
            "proxy_enabled": True,
            "secret_url": "https://private.example/customer",
        },
    }

    built = payload.build_payload(
        "init",
        stats_snapshot,
        event_id="event-1",
        trigger_ts="2026-05-03T00:00:00Z",
        version="1.2.3",
        platform_info={
            "os": "linux",
            "arch": "x86_64",
            "python": "3.12",
            "shell": "bash",
            "installer": "uv-tool",
            "containerized": False,
            "hostname": "private-host",
        },
        feature_capabilities={
            "glaas_endpoint_kind": "custom",
            "proxy_enabled": True,
            "ray_enabled": False,
            "osmo_enabled": False,
            "raw_endpoint": "https://private.example/customer",
        },
    )

    assert tuple(built) == payload.ALLOWED_TOP_LEVEL_FIELDS
    assert built["subcommand_counts"] == {"init": 1, "run": 3}
    assert built["tracer_selections"] == {"ptrace": 2}
    assert built["platform"] == {
        "os": "linux",
        "arch": "x86_64",
        "python": "3.12",
        "shell": "bash",
        "installer": "uv-tool",
        "containerized": False,
    }
    assert built["feature_capabilities"] == {
        "glaas_endpoint_kind": "custom",
        "proxy_enabled": True,
        "ray_enabled": False,
        "osmo_enabled": False,
    }

    encoded = json.dumps(built, sort_keys=True)
    forbidden_fragments = [
        "train.py",
        "customer",
        "/private",
        "API_KEY",
        "private-host",
        "raw_endpoint",
        "secret_command",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in encoded
