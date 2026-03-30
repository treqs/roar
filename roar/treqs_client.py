from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .auth_store import load_auth_state


class TreqsClientError(RuntimeError):
    pass


def fetch_access_context(treqs_api_url: str) -> dict[str, Any]:
    auth_state = load_auth_state()
    if auth_state is None or not auth_state.access_token:
        raise TreqsClientError("Not logged in. Run `roar login` before linking a project.")

    url = f"{treqs_api_url.rstrip('/')}/api/v1/user/access-context"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {auth_state.access_token}")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise TreqsClientError(
            f"Failed to fetch treqs access context: HTTP {exc.code} {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TreqsClientError(f"Failed to reach treqs API: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise TreqsClientError("Invalid treqs access-context response")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise TreqsClientError("Invalid treqs access-context payload")
    return data
