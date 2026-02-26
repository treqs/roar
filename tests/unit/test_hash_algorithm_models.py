"""Unit tests for hash algorithm model constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from roar.core.models.artifact import ArtifactHash
from roar.core.models.glaas import ArtifactHashRequest
from roar.core.models.run import RunArguments


def test_artifact_hash_allows_composite_blake3() -> None:
    model = ArtifactHash(algorithm="composite-blake3", digest="a" * 64)
    assert model.algorithm == "composite-blake3"


def test_glaas_artifact_hash_request_allows_composite_blake3() -> None:
    model = ArtifactHashRequest(algorithm="composite-blake3", digest="b" * 64)
    assert model.algorithm == "composite-blake3"


def test_run_arguments_still_rejects_composite_blake3() -> None:
    with pytest.raises(ValidationError):
        RunArguments(command=["echo", "ok"], hash_algorithms=["composite-blake3"])
