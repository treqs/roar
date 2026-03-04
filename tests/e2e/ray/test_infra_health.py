"""Infrastructure health gate checks for local Ray, GLaaS, and MinIO services."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest


def _skip_service_unreachable(service_name: str, url: str, exc: Exception) -> None:
    pytest.skip(f"{service_name} service not running: unable to reach {url}: {exc}")


def _http_get(url: str, service_name: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = int(response.getcode())
            body = response.read().decode("utf-8", errors="replace")
            return status, body
    except urllib.error.URLError as exc:
        _skip_service_unreachable(service_name, url, exc)
    except (TimeoutError, ConnectionError, OSError) as exc:
        _skip_service_unreachable(service_name, url, exc)


def _parse_json_or_fail(body: str, context: str) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{context}: expected JSON body but parsing failed ({exc}). Body: {body}")
    if not isinstance(payload, dict):
        pytest.fail(f"{context}: expected JSON object body. Body: {body}")
    return payload


def _looks_like_connection_error(output: str) -> bool:
    normalized = output.lower()
    indicators = (
        "connection refused",
        "failed to connect",
        "unable to connect",
        "cannot connect",
        "could not connect",
        "max retries exceeded",
        "connection error",
        "timed out",
        "deadline exceeded",
    )
    return any(indicator in normalized for indicator in indicators)


def _missing_ray_jobs_sdk(output: str) -> bool:
    normalized = output.lower()
    return "require the ray[default] installation" in normalized


@pytest.mark.e2e
def test_ray_head_dashboard_is_reachable() -> None:
    url = "http://localhost:8265/api/version"
    status, body = _http_get(url, "Ray dashboard")

    assert status == 200, f"Expected 200 from {url}, got {status}. Body: {body}"
    payload = _parse_json_or_fail(body, "Ray dashboard version endpoint")
    assert "ray_version" in payload, f"Expected 'ray_version' key in response. Body: {body}"


@pytest.mark.e2e
def test_ray_job_submission_works() -> None:
    command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--no-wait=false",
        "--",
        "python3",
        "-c",
        "import ray; ray.init(); print('HEALTH_OK'); ray.shutdown()",
    ]
    fallback_command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--",
        "python3",
        "-c",
        "import ray; ray.init(); print('HEALTH_OK'); ray.shutdown()",
    ]

    def _run_submit(run_command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                run_command,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(
                "Ray job submit timed out after 60 seconds. "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        except FileNotFoundError as exc:
            pytest.fail(f"ray CLI is not available: {exc}")

    result = _run_submit(command)
    output = f"{result.stdout}\n{result.stderr}"

    if "option '--no-wait' does not take a value" in output.lower():
        result = _run_submit(fallback_command)
        output = f"{result.stdout}\n{result.stderr}"

    if result.returncode != 0 and _missing_ray_jobs_sdk(output):
        pytest.skip(
            "Ray service not running: ray job submission dependencies are unavailable "
            "(ray[default] not installed)."
        )

    if result.returncode != 0 and _looks_like_connection_error(output):
        pytest.skip(
            "Ray service not running: job submission could not reach "
            f"http://localhost:8265. Output:\n{output}"
        )

    assert result.returncode == 0, (
        f"Expected ray job submit exit code 0, got {result.returncode}. Output:\n{output}"
    )
    assert "HEALTH_OK" in output, f"Expected HEALTH_OK marker in ray job output. Output:\n{output}"


@pytest.mark.e2e
def test_glaas_health_endpoint_responds() -> None:
    url = "http://localhost:3001/api/v1/health"
    status, body = _http_get(url, "GLaaS")

    assert status == 200, f"Expected 200 from {url}, got {status}. Body: {body}"
    payload = _parse_json_or_fail(body, "GLaaS health endpoint")
    assert payload.get("success") is True, f"Expected success=true in response. Body: {body}"


@pytest.mark.e2e
def test_minio_is_reachable() -> None:
    url = "http://localhost:9000/minio/health/live"
    status, body = _http_get(url, "MinIO")

    assert status == 200, f"Expected 200 from {url}, got {status}. Body: {body}"
