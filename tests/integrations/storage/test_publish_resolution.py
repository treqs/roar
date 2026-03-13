from __future__ import annotations

from unittest.mock import patch

import pytest

from roar.integrations.storage import NoOpBackend, resolve_publish_storage_backend


def test_resolve_publish_storage_backend_returns_memory_backend() -> None:
    backend = resolve_publish_storage_backend("memory://bucket/prefix")

    from roar.integrations.storage.memory import MemoryBackend

    assert isinstance(backend, MemoryBackend)


def test_resolve_publish_storage_backend_returns_noop_when_uploads_skipped() -> None:
    with patch("roar.integrations.storage.publish.should_skip_upload", return_value=True):
        backend = resolve_publish_storage_backend("s3://bucket/prefix")

    assert isinstance(backend, NoOpBackend)


def test_resolve_publish_storage_backend_rejects_unsupported_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported destination scheme"):
        resolve_publish_storage_backend("ftp://server/path")
