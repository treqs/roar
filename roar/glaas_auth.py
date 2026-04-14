from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .integrations.config import config_get

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_GLAAS_API_URL = "https://api.glaas.ai"


class GlaasAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceAuthorizationSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: float
    interval: float


@dataclass(frozen=True)
class DeviceTokenResponse:
    access_token: str
    refresh_token: str | None
    refresh_expires_at: str | None
    id_token: str | None
    token_type: str | None
    expires_in: float | None
    provider: str | None
    issuer: str | None
    client_id: str | None
    raw_data: dict[str, Any]


def resolve_auth_api_url(explicit_url: str | None = None) -> str:
    resolved = (
        (explicit_url or "").strip()
        or os.environ.get("GLAAS_API_URL", "").strip()
        or os.environ.get("ROAR_GLAAS__URL", "").strip()
        or str(config_get("glaas.url") or "").strip()
        or DEFAULT_GLAAS_API_URL
    ).rstrip("/")

    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GlaasAuthError(f"Invalid GLaaS API URL: {resolved}")

    if parsed.scheme == "http" and not _is_local_http_url(parsed.hostname):
        allow_http = os.environ.get("ROAR_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes"}
        if not allow_http:
            raise GlaasAuthError(
                f"Refusing to use non-local insecure HTTP GLaaS API URL: {resolved}. "
                "Use HTTPS, or set ROAR_ALLOW_HTTP=1 only for controlled development environments."
            )

    return resolved


def open_browser(url: str, opener: Callable[[str], bool] | None = None) -> bool:
    if os.environ.get("ROAR_DISABLE_BROWSER_OPEN", "").strip().lower() in {"1", "true", "yes"}:
        return False

    browser_opener = opener or (lambda target: bool(webbrowser.open(target, new=2)))
    try:
        return bool(browser_opener(url))
    except Exception:
        return False


def start_device_authorization(
    auth_api_url: str,
    *,
    client_id: str = "roar-cli",
    client_name: str = "roar-cli",
) -> DeviceAuthorizationSession:
    payload = _request_json(
        f"{auth_api_url.rstrip('/')}/api/v1/auth/device/start",
        method="POST",
        payload={"client_id": client_id, "client_name": client_name},
    )
    data = _unwrap_success_payload(payload, context="device authorization start")

    device_code = _required_string(data.get("device_code"), field_name="device_code")
    user_code = _required_string(data.get("user_code"), field_name="user_code")
    verification_uri = _required_string(data.get("verification_uri"), field_name="verification_uri")
    verification_uri_complete = _optional_string(data.get("verification_uri_complete"))
    expires_in = _coerce_float(data.get("expires_in"), default=600.0)
    interval = _coerce_float(data.get("interval"), default=5.0)
    return DeviceAuthorizationSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        expires_in=expires_in,
        interval=interval,
    )


def poll_device_token(
    auth_api_url: str,
    session: DeviceAuthorizationSession,
    *,
    client_id: str = "roar-cli",
    sleep: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> DeviceTokenResponse:
    deadline = time_fn() + max(session.expires_in, 1.0)
    interval = max(session.interval, 0.0)

    while True:
        if time_fn() >= deadline:
            raise GlaasAuthError("Device authorization timed out before approval completed")

        try:
            payload = _request_json(
                f"{auth_api_url.rstrip('/')}/api/v1/auth/device/token",
                method="POST",
                payload={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "device_code": session.device_code,
                    "client_id": client_id,
                },
            )
        except _GlaasApiResponseError as exc:
            error_code, message = _extract_error_details(exc.payload)
            if error_code == "authorization_pending":
                sleep(interval)
                continue
            if error_code == "slow_down":
                interval += 5.0
                sleep(interval)
                continue
            if error_code == "expired_token":
                raise GlaasAuthError(
                    "Device authorization expired. Run `roar login` again."
                ) from exc
            if error_code == "access_denied":
                raise GlaasAuthError(message or "Device authorization was denied.") from exc
            if message:
                raise GlaasAuthError(message) from exc
            raise GlaasAuthError(f"Device token exchange failed with HTTP {exc.status}") from exc

        data = _unwrap_success_payload(payload, context="device token exchange")
        status = _optional_string(data.get("status"))
        if status == "authorization_pending":
            interval = _coerce_float(data.get("interval"), default=interval)
            sleep(interval)
            continue
        if status not in {None, "approved"}:
            raise GlaasAuthError(f"Unexpected device authorization status from auth API: {status}")

        access_token = _optional_string(data.get("access_token")) or _optional_string(
            data.get("token")
        )
        if access_token is None:
            raise GlaasAuthError("Invalid auth API response: missing access_token")

        expires_in = _coerce_optional_float(data.get("expires_in"))
        if expires_in is None:
            expires_in = _expires_in_from_iso8601(data.get("expires_at"), now=time.time())

        return DeviceTokenResponse(
            access_token=access_token,
            refresh_token=_optional_string(data.get("refresh_token")),
            refresh_expires_at=_optional_string(data.get("refresh_expires_at")),
            id_token=_optional_string(data.get("id_token")),
            token_type=_optional_string(data.get("token_type")) or "Bearer",
            expires_in=expires_in,
            provider=_optional_string(data.get("provider")),
            issuer=_optional_string(data.get("issuer")),
            client_id=_optional_string(data.get("client_id")) or client_id,
            raw_data=data,
        )


def build_auth_state_from_device_login(
    token_response: DeviceTokenResponse,
    access_context: dict[str, Any],
) -> dict[str, Any]:
    user = access_context.get("user")
    if not isinstance(user, dict):
        raise GlaasAuthError("Invalid auth access-context payload: missing user")

    expires_at = None
    if token_response.expires_in is not None:
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + max(token_response.expires_in, 0.0)),
        )

    return {
        "version": 1,
        "provider": token_response.provider or "treqs-device",
        "issuer": token_response.issuer,
        "client_id": token_response.client_id,
        "access_token": token_response.access_token,
        "refresh_token": token_response.refresh_token,
        "refresh_expires_at": token_response.refresh_expires_at,
        "id_token": token_response.id_token,
        "expires_at": expires_at,
        "user": {
            "sub": _optional_string(user.get("sub")) or "",
            "db_user_id": _optional_string(user.get("id")),
            "email": _optional_string(user.get("email")),
            "username": _optional_string(user.get("username")),
        },
    }


def refresh_access_token(
    auth_api_url: str,
    refresh_token: str,
    *,
    client_id: str = "roar-cli",
) -> DeviceTokenResponse:
    token = refresh_token.strip()
    if not token:
        raise GlaasAuthError("Missing refresh token")

    payload = _request_json(
        f"{auth_api_url.rstrip('/')}/api/v1/auth/refresh",
        method="POST",
        payload={"refresh_token": token, "client_id": client_id},
    )
    data = _unwrap_success_payload(payload, context="token refresh")
    access_token = _optional_string(data.get("access_token")) or _optional_string(data.get("token"))
    if access_token is None:
        raise GlaasAuthError("Invalid auth API response: missing access_token")

    expires_in = _coerce_optional_float(data.get("expires_in"))
    if expires_in is None:
        expires_in = _expires_in_from_iso8601(data.get("expires_at"), now=time.time())

    return DeviceTokenResponse(
        access_token=access_token,
        refresh_token=_optional_string(data.get("refresh_token")),
        refresh_expires_at=_optional_string(data.get("refresh_expires_at")),
        id_token=_optional_string(data.get("id_token")),
        token_type=_optional_string(data.get("token_type")) or "Bearer",
        expires_in=expires_in,
        provider=_optional_string(data.get("provider")),
        issuer=_optional_string(data.get("issuer")),
        client_id=_optional_string(data.get("client_id")) or client_id,
        raw_data=data,
    )


def revoke_token(auth_api_url: str, access_token: str) -> None:
    token = access_token.strip()
    if not token:
        return

    _request_json(
        f"{auth_api_url.rstrip('/')}/api/v1/auth/logout",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )


def fetch_access_context_via_auth_api(glaas_api_url: str, *, access_token: str) -> dict[str, Any]:
    token = access_token.strip()
    if not token:
        raise GlaasAuthError("Missing access token")

    base_url = glaas_api_url.rstrip("/")
    auth_endpoint = f"{base_url}/api/v1/auth/access-context"
    legacy_endpoint = f"{base_url}/api/v1/user/access-context"

    try:
        payload = _request_json(
            auth_endpoint,
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
    except _GlaasApiResponseError as exc:
        if exc.status == 404:
            try:
                payload = _request_json(
                    legacy_endpoint,
                    method="GET",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except _GlaasApiResponseError as fallback_exc:
                message = _extract_error_details(fallback_exc.payload)[1]
                if message:
                    raise GlaasAuthError(message) from fallback_exc
                raise GlaasAuthError(
                    f"Failed to fetch GLaaS auth access context: HTTP {fallback_exc.status}"
                ) from fallback_exc
        else:
            _error_code, message = _extract_error_details(exc.payload)
            if message:
                raise GlaasAuthError(message) from exc
            raise GlaasAuthError(
                f"Failed to fetch GLaaS auth access context: HTTP {exc.status}"
            ) from exc

    data = _unwrap_success_payload(payload, context="auth access context")
    user = data.get("user")
    if not isinstance(user, dict):
        raise GlaasAuthError("Invalid GLaaS auth access-context payload: missing user")
    return data


class _GlaasApiResponseError(GlaasAuthError):
    def __init__(self, status: int, payload: dict[str, Any] | None) -> None:
        self.status = status
        self.payload = payload or {}
        super().__init__(f"HTTP {status}")


def _request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_body = response.read().decode("utf-8")
            data = json.loads(raw_body) if raw_body else {}
    except urllib.error.HTTPError as exc:
        payload_data = _decode_error_payload(exc)
        raise _GlaasApiResponseError(exc.code, payload_data) from exc
    except urllib.error.URLError as exc:
        raise GlaasAuthError(f"Failed to reach auth API at {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GlaasAuthError(f"Invalid JSON response from auth API at {url}") from exc

    if not isinstance(data, dict):
        raise GlaasAuthError(f"Invalid JSON object response from auth API at {url}")
    return data


def _decode_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any] | None:
    try:
        raw_body = exc.read().decode("utf-8")
    except Exception:
        return None
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"error": {"message": raw_body}}
    return payload if isinstance(payload, dict) else {"error": {"message": raw_body}}


def _unwrap_success_payload(payload: dict[str, Any], *, context: str) -> dict[str, Any]:
    if payload.get("success") is True:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        raise GlaasAuthError(f"Invalid auth API {context} response payload")
    return payload


def _extract_error_details(payload: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None

    error = payload.get("error")
    if isinstance(error, str):
        return error.strip() or None, _optional_string(payload.get("error_description"))
    if isinstance(error, dict):
        code = _optional_string(error.get("code")) or _optional_string(error.get("type"))
        message = _optional_string(error.get("message")) or _optional_string(error.get("detail"))
        return code, message

    code = _optional_string(payload.get("code")) or _optional_string(payload.get("error_code"))
    message = _optional_string(payload.get("message")) or _optional_string(payload.get("detail"))
    return code, message


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _required_string(value: Any, *, field_name: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise GlaasAuthError(f"Invalid auth API response: missing {field_name}")
    return normalized


def _coerce_float(value: Any, *, default: float) -> float:
    parsed = _coerce_optional_float(value)
    return default if parsed is None else parsed


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _optional_string(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise GlaasAuthError(f"Invalid numeric value in auth API response: {text}") from exc


def _expires_in_from_iso8601(value: Any, *, now: float) -> float | None:
    text = _optional_string(value)
    if text is None:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        expires_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GlaasAuthError(f"Invalid expires_at timestamp in auth API response: {text}") from exc

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    remaining = expires_at.timestamp() - now
    return max(0.0, remaining)


def _is_local_http_url(hostname: str | None) -> bool:
    if hostname is None:
        return False
    normalized = hostname.strip().lower()
    return normalized in {"localhost", "127.0.0.1", "::1"}
