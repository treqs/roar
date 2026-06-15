"""Unit tests for hash algorithm model constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from roar.core.models.run import RunArguments


def test_run_arguments_still_rejects_composite_blake3() -> None:
    with pytest.raises(ValidationError):
        RunArguments(command=["echo", "ok"], hash_algorithms=["composite-blake3"])
