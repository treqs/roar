"""Product-path coverage for Roar telemetry upload behavior."""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from roar.telemetry import paths, queue, uploader

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="product run path uses Linux tracer"),
]


_SUPPRESSION_ENV_OVERRIDES = {
    "CI": "",
    "GITHUB_ACTIONS": "",
    "GITLAB_CI": "",
    "BUILDKITE": "",
    "CIRCLECI": "",
    "TRAVIS": "",
    "APPVEYOR": "",
    "TEAMCITY_VERSION": "",
    "JENKINS_URL": "",
    "TF_BUILD": "",
    "BITBUCKET_BUILD_NUMBER": "",
    "CODEBUILD_BUILD_ID": "",
    "DRONE": "",
    "HEROKU_TEST_RUN_ID": "",
    "PYTEST_CURRENT_TEST": "",
    "DO_NOT_TRACK": "",
    "ROAR_NO_TELEMETRY": "",
}


class _TelemetryCollector(ThreadingHTTPServer):
    received_payloads: list[dict[str, Any]]
    received_paths: list[str]


def _start_collector() -> tuple[_TelemetryCollector, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length)
            self.server.received_paths.append(self.path)  # type: ignore[attr-defined]
            self.server.received_payloads.append(json.loads(raw_body))  # type: ignore[attr-defined]
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = _TelemetryCollector(("127.0.0.1", 0), Handler)
    server.received_payloads = []
    server.received_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait_for_payloads(server: _TelemetryCollector, expected: int = 1) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(server.received_payloads) >= expected:
            return list(server.received_payloads)
        time.sleep(0.05)
    return list(server.received_payloads)


def test_roar_run_queues_and_uploads_telemetry_product_path(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
) -> None:
    telemetry_root = temp_git_repo.parent / "telemetry-state"
    endpoint_path = "/api/v1/telemetry/roar"
    server, thread = _start_collector()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}{endpoint_path}"

    script = temp_git_repo / "train.py"
    script.write_text(
        """
from pathlib import Path

Path("model.txt").write_text("ok\\n", encoding="utf-8")
print("trained")
""".lstrip(),
        encoding="utf-8",
    )
    git_commit("Add telemetry product path script")

    env = {
        **_SUPPRESSION_ENV_OVERRIDES,
        "HOME": str(telemetry_root / "home"),
        "XDG_CONFIG_HOME": str(telemetry_root / "config"),
        "XDG_CACHE_HOME": str(telemetry_root / "cache"),
        "ROAR_TELEMETRY_ENDPOINT": endpoint,
    }

    try:
        result = roar_cli(
            "run",
            "--tracer",
            "ptrace",
            sys.executable,
            "train.py",
            check=False,
            env_overrides=env,
        )
        assert result.returncode == 0, (
            f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        resolved_paths = paths.resolve_paths(env)
        uploader.drain_queue(environ=env, paths=resolved_paths)
        payloads = _wait_for_payloads(server, expected=1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payloads, "roar run did not upload any telemetry payloads"
    assert all(path == endpoint_path for path in server.received_paths)
    assert queue.queued_event_count(resolved_paths) == 0

    successful_run_payloads = [
        payload for payload in payloads if payload["trigger"] == "first_successful_roar_run"
    ]
    assert successful_run_payloads, payloads

    payload = successful_run_payloads[-1]
    assert payload["payload_schema_version"] == 1
    assert payload["stats_schema_version"] == 1
    assert payload["install_id"]
    assert payload["subcommand_counts"]["run"] == 1
    assert payload["subcommand_counts"]["run_success"] == 1
    assert payload["tracer_selections"]["ptrace"] == 1
    assert payload["feature_capabilities"]["glaas_endpoint_kind"] == "default_dev"
    assert "command" not in payload
    assert "path" not in payload
