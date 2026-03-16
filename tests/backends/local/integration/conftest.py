"""Local backend integration fixtures."""

import sys

import pytest


@pytest.fixture
def python_exe() -> str:
    """Return the active Python interpreter for local backend integration tests."""
    return sys.executable
