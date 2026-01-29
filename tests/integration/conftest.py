"""Integration test fixtures — re-export shared fixtures."""

import sys

import pytest


@pytest.fixture
def python_exe() -> str:
    """Return the absolute path to the Python executable."""
    return sys.executable
