"""HTTP transport helpers for GLaaS API operations."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any


def _get_logger():
    from ..core.logging import get_logger

    return get_logger()


def parse_json_response(response_body: str, http_status: int) -> tuple[Any | None, str | None]:
    """Parse JSON response with descriptive error messages."""
    if not response_body:
        return None, f"Server returned empty response (HTTP {http_status})"

    if not response_body.strip():
        return None, f"Server returned whitespace-only response (HTTP {http_status})"

    stripped = response_body.strip()
    if stripped.startswith("<!") or stripped.lower().startswith("<html"):
        preview = response_body[:100].replace("\n", " ")
        return None, f"Server returned HTML instead of JSON: '{preview}...'"

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as e:
        preview = response_body[:100].replace("\n", " ")
        return None, (
            f"Invalid JSON in response (HTTP {http_status}) at position {e.pos}: '{preview}...'"
        )

    return parsed, None


def request_json(
    *,
    base_url: str,
    method: str,
    path: str,
    body: dict | None,
    auth_header_factory: Callable[[str, str, bytes | None], str | None],
) -> tuple[Any | None, str | None]:
    """
    Make an authenticated JSON request.

    Returns (response_dict, error_message).
    """
    url = f"{base_url}{path}"
    body_bytes = json.dumps(body).encode() if body else None

    _get_logger().debug(
        "API request: %s %s (body: %d bytes)",
        method,
        url,
        len(body_bytes) if body_bytes else 0,
    )

    auth_header = auth_header_factory(method, path, body_bytes)

    def _build_request(auth_value: str | None) -> urllib.request.Request:
        req = urllib.request.Request(url, data=body_bytes, method=method)
        if auth_value:
            req.add_header("Authorization", auth_value)
        if body_bytes:
            req.add_header("Content-Type", "application/json")
        return req

    def _perform_request(auth_value: str | None) -> tuple[Any | None, str | None, int | None]:
        req = _build_request(auth_value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                http_status = resp.status
                response_body = resp.read().decode()
                _get_logger().debug(
                    "API response: %s %s -> HTTP %d (%d bytes)",
                    method,
                    path,
                    http_status,
                    len(response_body),
                )

                if not response_body or not response_body.strip():
                    return {}, None, http_status

                result, error = parse_json_response(response_body, http_status)
                if error:
                    return None, error, http_status

                if isinstance(result, dict) and result.get("success") and "data" in result:
                    return result["data"], None, http_status
                return result, None, http_status
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            error_data, _ = parse_json_response(error_body, e.code)
            if error_data and isinstance(error_data, dict):
                detail = error_data.get("detail") or error_data.get("message") or str(e)
            elif error_body:
                stripped = error_body.strip()
                if e.code == 403 and (
                    stripped.startswith("<!") or stripped.lower().startswith("<html")
                ):
                    detail = (
                        "Access denied by proxy or firewall (received HTML 403). "
                        "Check network configuration."
                    )
                else:
                    preview = error_body[:100].replace("\n", " ")
                    detail = (
                        f"Non-JSON response: '{preview}...'"
                        if len(error_body) > 100
                        else error_body
                    )
            else:
                detail = str(e)
            _get_logger().debug(
                "API error: %s %s -> HTTP %d: %s", method, path, e.code, detail[:200]
            )
            return None, f"HTTP {e.code}: {detail}", e.code
        except urllib.error.URLError as e:
            _get_logger().debug("GLaaS connection error to %s: %s", url, e)
            return None, f"Connection error: {e}", None
        except json.JSONDecodeError as e:
            _get_logger().debug(
                "GLaaS invalid JSON response from %s at position %d: %s", url, e.pos, e.msg
            )
            return None, f"Invalid JSON response at position {e.pos}: {e.msg}", None
        except Exception as e:
            _get_logger().debug("GLaaS request to %s failed: %s", url, e)
            return None, str(e), None

    result, error, status_code = _perform_request(auth_header)
    if error is None:
        return result, None

    if auth_header and status_code == 401:
        _get_logger().debug(
            "GLaaS optional-auth fallback: retrying %s %s without Authorization after 401",
            method,
            path,
        )
        retry_result, retry_error, _retry_status = _perform_request(None)
        if retry_error is None:
            return retry_result, None
        return retry_result, retry_error

    return result, error
