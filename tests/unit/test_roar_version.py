"""``roar.__version__`` must be importable and resolve to a real string.

The lazy-install path imports ``from roar import __version__`` inside the
tracer launcher. An empty ``roar/__init__.py`` would silently turn the
entire lazy-install codepath into dead code via the launcher's
ImportError fallback. This test catches that regression at the package
level so it can't slip through.
"""

from __future__ import annotations


def test_version_is_importable_and_non_empty() -> None:
    from roar import __version__

    assert isinstance(__version__, str)
    assert __version__
    assert __version__ != "0.0.0+unknown" or not _roar_cli_metadata_available()


def _roar_cli_metadata_available() -> bool:
    """True if PyPI/wheel metadata for roar-cli is reachable from this Python."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        version("roar-cli")
        return True
    except PackageNotFoundError:
        return False
