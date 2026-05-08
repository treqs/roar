"""HTTP transport helpers for GLaaS API operations."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from threading import Lock
from typing import Any, Literal

_AUTH_PROBE_PATH = "/api/v1/sessions?limit=1"
_AuthMode = Literal["unknown", "authenticated", "anonymous"]
_AUTH_MODE_BY_BASE_URL: dict[str, _AuthMode] = {}
_AUTH_MODE_LOCK = Lock()


def _get_logger():
    from ...core.logging import get_logger

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


def _extract_error_detail(error_data: Any, fallback: str) -> str:
    if isinstance(error_data, dict):
        detail = error_data.get("detail") or error_data.get("message")
        nested_error = error_data.get("error")
        if not detail and isinstance(nested_error, dict):
            detail = nested_error.get("detail") or nested_error.get("message")
        if detail:
            return str(detail)
    return fallback


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _get_cached_auth_mode(base_url: str) -> _AuthMode:
    normalized = _normalize_base_url(base_url)
    with _AUTH_MODE_LOCK:
        return _AUTH_MODE_BY_BASE_URL.get(normalized, "unknown")


def _set_cached_auth_mode(base_url: str, mode: _AuthMode) -> None:
    normalized = _normalize_base_url(base_url)
    with _AUTH_MODE_LOCK:
        _AUTH_MODE_BY_BASE_URL[normalized] = mode


def _mark_anonymous(base_url: str, detail: str) -> None:
    normalized = _normalize_base_url(base_url)
    with _AUTH_MODE_LOCK:
        previous = _AUTH_MODE_BY_BASE_URL.get(normalized, "unknown")
        _AUTH_MODE_BY_BASE_URL[normalized] = "anonymous"

    if previous != "anonymous":
        _get_logger().warning(
            "GLaaS authentication failed for %s; falling back to anonymous requests for this process. %s",
            normalized,
            detail,
        )


def reset_auth_mode_cache() -> None:
    """Reset cached per-server auth mode for tests and fresh processes."""
    with _AUTH_MODE_LOCK:
        _AUTH_MODE_BY_BASE_URL.clear()


def get_cached_auth_mode(base_url: str) -> _AuthMode:
    """Return the cached optional-auth mode for a GLaaS base URL."""
    return _get_cached_auth_mode(base_url)


def probe_auth_header(
    *,
    base_url: str,
    auth_header_factory: Callable[[str, str, bytes | None], str | None],
) -> _AuthMode:
    """Probe whether optional SSH auth is accepted for a GLaaS base URL."""
    auth_mode = _get_cached_auth_mode(base_url)
    if auth_mode != "unknown":
        return auth_mode

    auth_header = auth_header_factory("GET", _AUTH_PROBE_PATH, None)
    if not auth_header:
        return "anonymous"

    normalized = _normalize_base_url(base_url)
    request_url = f"{normalized}{_AUTH_PROBE_PATH}"
    req = urllib.request.Request(request_url, method="GET")
    req.add_header("Authorization", auth_header)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _get_logger().debug(
                "GLaaS auth probe: GET %s -> HTTP %d",
                _AUTH_PROBE_PATH,
                resp.status,
            )
            _set_cached_auth_mode(base_url, "authenticated")
            return "authenticated"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            _get_logger().debug(
                "GLaaS auth probe: GET %s -> HTTP 401",
                _AUTH_PROBE_PATH,
            )
            _mark_anonymous(base_url, "The configured SSH key was rejected by the server.")
            return "anonymous"
        _get_logger().debug(
            "GLaaS auth probe for %s was inconclusive: HTTP %d",
            normalized,
            exc.code,
        )
        return "unknown"
    except urllib.error.URLError as exc:
        _get_logger().debug("GLaaS auth probe connection error to %s: %s", request_url, exc)
        return "unknown"


def request_json(
    *,
    base_url: str,
    method: str,
    path: str,
    body: dict | None,
    auth_header_factory: Callable[[str, str, bytes | None], str | None],
    auth_header_value: str | None = None,
    allow_auth_fallback: bool = True,
) -> tuple[Any | None, str | None]:
    """
    Make an authenticated JSON request.

    Returns (response_dict, error_message).
    """
    url = f"{base_url}{path}"
    body_bytes = json.dumps(body).encode() if body else None

    def _perform_request(
        *,
        request_method: str,
        request_path: str,
        request_url: str,
        request_body: bytes | None,
        auth_value: str | None,
    ) -> tuple[Any | None, str | None, int | None]:
        _get_logger().debug(
            "API request: %s %s (body: %d bytes)",
            request_method,
            request_url,
            len(request_body) if request_body else 0,
        )
        req = urllib.request.Request(request_url, data=request_body, method=request_method)
        if auth_value:
            req.add_header("Authorization", auth_value)
        if request_body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                http_status = resp.status
                response_body = resp.read().decode()
                _get_logger().debug(
                    "API response: %s %s -> HTTP %d (%d bytes)",
                    request_method,
                    request_path,
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
                detail = _extract_error_detail(error_data, str(e))
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
                "API error: %s %s -> HTTP %d: %s",
                request_method,
                request_path,
                e.code,
                detail[:200],
            )
            return None, f"HTTP {e.code}: {detail}", e.code
        except urllib.error.URLError as e:
            _get_logger().debug("GLaaS connection error to %s: %s", request_url, e)
            return None, f"Connection error: {e}", None
        except json.JSONDecodeError as e:
            _get_logger().debug(
                "GLaaS invalid JSON response from %s at position %d: %s",
                request_url,
                e.pos,
                e.msg,
            )
            return None, f"Invalid JSON response at position {e.pos}: {e.msg}", None
        except Exception as e:
            _get_logger().debug("GLaaS request to %s failed: %s", request_url, e)
            return None, str(e), None

    auth_mode = _get_cached_auth_mode(base_url)
    auth_header = auth_header_value
    if auth_header is None:
        auth_header = (
            None if auth_mode == "anonymous" else auth_header_factory(method, path, body_bytes)
        )
    bearer_auth = bool(auth_header and auth_header.startswith("Bearer "))

    if allow_auth_fallback and auth_mode == "unknown" and auth_header and not bearer_auth:
        probe_auth_header = auth_header_factory("GET", _AUTH_PROBE_PATH, None)
        if probe_auth_header:
            _probe_result, probe_error, probe_status = _perform_request(
                request_method="GET",
                request_path=_AUTH_PROBE_PATH,
                request_url=f"{base_url}{_AUTH_PROBE_PATH}",
                request_body=None,
                auth_value=probe_auth_header,
            )
            if probe_error is None:
                _set_cached_auth_mode(base_url, "authenticated")
            elif probe_status == 401:
                _mark_anonymous(base_url, "The configured SSH key was rejected by the server.")
                auth_header = None
            else:
                _get_logger().debug(
                    "GLaaS auth probe for %s was inconclusive; proceeding with request auth path: %s",
                    base_url,
                    probe_error,
                )

    result, error, status_code = _perform_request(
        request_method=method,
        request_path=path,
        request_url=url,
        request_body=body_bytes,
        auth_value=auth_header,
    )
    if error is None:
        return result, None

    if allow_auth_fallback and auth_header and status_code == 401 and not bearer_auth:
        _mark_anonymous(base_url, f"The server returned HTTP 401 for {method} {path}.")
        _get_logger().debug(
            "GLaaS optional-auth fallback: retrying %s %s without Authorization after 401",
            method,
            path,
        )
        retry_result, retry_error, _retry_status = _perform_request(
            request_method=method,
            request_path=path,
            request_url=url,
            request_body=body_bytes,
            auth_value=None,
        )
        if retry_error is None:
            return retry_result, None
        return retry_result, retry_error

    return result, error
