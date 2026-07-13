"""Tests for `lookup_remote_artifact`'s authenticated-then-public fallback."""

from __future__ import annotations

from roar.application.lookup.remote_artifacts import lookup_remote_artifact
from roar.core.exceptions import GlaasApiError


class _FakeReader:
    """A `SupportsArtifactLookup`-shaped double with call tracking."""

    def __init__(
        self,
        *,
        get_artifact_result=None,
        get_artifact_error: GlaasApiError | Exception | None = None,
        get_public_artifact_result=None,
        get_public_artifact_error: GlaasApiError | Exception | None = None,
    ) -> None:
        self._get_artifact_result = get_artifact_result
        self._get_artifact_error = get_artifact_error
        self._get_public_artifact_result = get_public_artifact_result
        self._get_public_artifact_error = get_public_artifact_error
        self.get_artifact_calls: list[str] = []
        self.get_public_artifact_calls: list[str] = []

    def get_artifact(self, hash_prefix: str):
        self.get_artifact_calls.append(hash_prefix)
        if self._get_artifact_error is not None:
            raise self._get_artifact_error
        return self._get_artifact_result

    def get_public_artifact(self, hash_prefix: str):
        self.get_public_artifact_calls.append(hash_prefix)
        if self._get_public_artifact_error is not None:
            raise self._get_public_artifact_error
        return self._get_public_artifact_result


class _FakeReaderWithoutPublicFallback:
    """A minimal reader with no `get_public_artifact` at all — simulates an
    older test double built before the public-endpoint fallback existed."""

    def __init__(self, *, get_artifact_error: GlaasApiError) -> None:
        self._get_artifact_error = get_artifact_error

    def get_artifact(self, hash_prefix: str):
        raise self._get_artifact_error


def test_lookup_remote_artifact_returns_authenticated_result_without_public_fallback() -> None:
    reader = _FakeReader(get_artifact_result={"hash": "abc123"})

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact == {"hash": "abc123"}
    assert error is None
    assert reader.get_public_artifact_calls == []


def test_lookup_remote_artifact_404_is_a_clean_miss() -> None:
    reader = _FakeReader(get_artifact_error=GlaasApiError("not found", status_code=404))

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact is None
    assert error is None
    assert reader.get_public_artifact_calls == []


def test_lookup_remote_artifact_falls_back_to_public_endpoint_on_401() -> None:
    reader = _FakeReader(
        get_artifact_error=GlaasApiError("unauthorized", status_code=401),
        get_public_artifact_result={"hash": "abc123", "visibility": "public"},
    )

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact == {"hash": "abc123", "visibility": "public"}
    assert error is None
    assert reader.get_public_artifact_calls == ["abc123"]


def test_lookup_remote_artifact_falls_back_to_public_endpoint_on_403() -> None:
    reader = _FakeReader(
        get_artifact_error=GlaasApiError("forbidden", status_code=403),
        get_public_artifact_result={"hash": "abc123"},
    )

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact == {"hash": "abc123"}
    assert error is None
    assert reader.get_public_artifact_calls == ["abc123"]


def test_lookup_remote_artifact_public_fallback_404_is_a_clean_miss() -> None:
    reader = _FakeReader(
        get_artifact_error=GlaasApiError("unauthorized", status_code=401),
        get_public_artifact_error=GlaasApiError("not found", status_code=404),
    )

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact is None
    assert error is None


def test_lookup_remote_artifact_public_fallback_error_propagates() -> None:
    reader = _FakeReader(
        get_artifact_error=GlaasApiError("unauthorized", status_code=401),
        get_public_artifact_error=GlaasApiError("server exploded", status_code=500),
    )

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact is None
    assert error is not None
    assert "server exploded" in error


def test_lookup_remote_artifact_other_error_codes_do_not_fall_back() -> None:
    reader = _FakeReader(get_artifact_error=GlaasApiError("server exploded", status_code=500))

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact is None
    assert error is not None
    assert "server exploded" in error
    assert reader.get_public_artifact_calls == []


def test_lookup_remote_artifact_does_not_fall_back_without_public_support() -> None:
    """A reader that doesn't implement get_public_artifact (e.g. an older
    test double) should degrade to the pre-fallback behavior, not crash."""
    reader = _FakeReaderWithoutPublicFallback(
        get_artifact_error=GlaasApiError("unauthorized", status_code=401),
    )

    artifact, error = lookup_remote_artifact(hash_prefix="abc123", artifact_reader=reader)

    assert artifact is None
    assert error is not None
    assert "unauthorized" in error
