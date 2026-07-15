"""Unit tests for the fragment-Secret cleanup selection logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from roar.backends.k8s.secret_cleanup import DEFAULT_MAX_AGE_SECONDS, select_expired_secrets

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _secret(name: str, namespace: str, age_seconds: int) -> dict:
    created = NOW - timedelta(seconds=age_seconds)
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def test_selects_only_aged_fragment_secrets() -> None:
    items = [
        _secret("roar-fragment-old00000", "ml", DEFAULT_MAX_AGE_SECONDS + 3600),
        _secret("roar-fragment-fresh000", "ml", 3600),
    ]

    expired = select_expired_secrets(items, max_age_seconds=DEFAULT_MAX_AGE_SECONDS, now=NOW)

    assert expired == [("ml", "roar-fragment-old00000")]


def test_never_touches_non_fragment_secrets_even_if_labeled() -> None:
    items = [
        _secret("some-other-secret", "ml", DEFAULT_MAX_AGE_SECONDS * 10),
    ]

    assert select_expired_secrets(items, max_age_seconds=1, now=NOW) == []


def test_skips_malformed_entries() -> None:
    items = [
        {"metadata": {"name": "roar-fragment-x", "namespace": ""}},
        {"metadata": {"name": "roar-fragment-y", "namespace": "ml"}},  # no timestamp
        {"metadata": {"name": "roar-fragment-z", "namespace": "ml", "creationTimestamp": "bogus"}},
    ]

    assert select_expired_secrets(items, max_age_seconds=1, now=NOW) == []
