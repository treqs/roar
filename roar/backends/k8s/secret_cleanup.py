"""Delete aged roar fragment-credential Secrets (webhook CronJob entrypoint).

Credential Secrets cannot carry an ownerReference: at CREATE admission the
workload has no UID yet, so Kubernetes garbage collection can never adopt
them. This module lists roar-managed Secrets cluster-wide and deletes those
older than ROAR_SECRET_MAX_AGE_SECONDS (default 7 days).

Age is a heuristic, not a session-liveness signal: sessions renew on 403
and can outlive any fixed TTL. Running containers are unaffected (env
resolves at pod start), but retry/scale-up pods referencing a deleted
Secret cannot start, and cluster-based `roar k8s attach` recovery is
lost. The chart therefore ships this CronJob disabled by default; its
ServiceAccount needs cluster-wide list/delete on Secrets (list responses
include Secret data), so treat it as a privileged component. See the
chart values for the full trade-off.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

_SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_API_BASE = "https://kubernetes.default.svc"
_MANAGED_BY_SELECTOR = "app.kubernetes.io/managed-by=roar"
_SECRET_NAME_PREFIX = "roar-fragment-"
DEFAULT_MAX_AGE_SECONDS = 604800


def _api_request(path: str, method: str = "GET") -> dict[str, Any]:
    with open(f"{_SERVICE_ACCOUNT_DIR}/token", encoding="utf-8") as handle:
        token = handle.read().strip()
    request = urllib.request.Request(
        url=f"{_API_BASE}{path}",
        headers={"authorization": f"Bearer {token}"},
        method=method,
    )
    context = ssl.create_default_context(cafile=f"{_SERVICE_ACCOUNT_DIR}/ca.crt")
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def select_expired_secrets(
    items: list[dict[str, Any]],
    *,
    max_age_seconds: int,
    now: datetime,
) -> list[tuple[str, str]]:
    """Return (namespace, name) for roar fragment Secrets older than max age."""
    expired: list[tuple[str, str]] = []
    for item in items:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if not name.startswith(_SECRET_NAME_PREFIX) or not namespace:
            continue
        created = _parse_timestamp(metadata.get("creationTimestamp"))
        if created is None:
            continue
        if (now - created).total_seconds() > max_age_seconds:
            expired.append((namespace, name))
    return expired


def main() -> int:
    max_age = int(os.environ.get("ROAR_SECRET_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
    selector = urllib.parse.quote(_MANAGED_BY_SELECTOR, safe="=")
    listing = _api_request(f"/api/v1/secrets?labelSelector={selector}")
    items = [item for item in listing.get("items") or [] if isinstance(item, dict)]

    expired = select_expired_secrets(items, max_age_seconds=max_age, now=datetime.now(timezone.utc))
    failures = 0
    for namespace, name in expired:
        try:
            _api_request(f"/api/v1/namespaces/{namespace}/secrets/{name}", method="DELETE")
            print(f"[roar-secret-cleanup] deleted {namespace}/{name}")
        except Exception as exc:
            failures += 1
            print(f"[roar-secret-cleanup] failed to delete {namespace}/{name}: {exc}")

    print(
        f"[roar-secret-cleanup] scanned {len(items)} secret(s), "
        f"deleted {len(expired) - failures}, failed {failures}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
