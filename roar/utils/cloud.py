"""
Cloud storage utilities.

Pure utility functions for cloud URL parsing and CLI availability checking.
These functions have no provider dependencies and can be used independently.
"""

from urllib.parse import urlparse


def parse_cloud_url(url: str) -> tuple[str, str, str]:
    """
    Parse a cloud URL into (scheme, bucket, key).

    Args:
        url: Cloud storage URL

    Returns:
        Tuple of (scheme, bucket, key) where key may be empty or end with /

    Raises:
        ValueError: If scheme is not supported
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    # No cloud schemes currently supported
    raise ValueError(f"Unsupported cloud scheme: {scheme}")


def is_directory_url(url: str) -> bool:
    """
    Check if a URL represents a directory (ends with /).

    Args:
        url: Cloud storage URL

    Returns:
        True if URL represents a directory
    """
    return url.rstrip("/") != url or not urlparse(url).path.split("/")[-1]


def check_cli_available(scheme: str) -> tuple[bool, str]:
    """
    Check if the required CLI tool is available for a cloud scheme.

    Args:
        scheme: Cloud scheme

    Returns:
        Tuple of (available, tool_name)
    """
    return False, f"unknown-{scheme}"


def get_cli_install_hint(scheme: str) -> str:
    """
    Get installation hint for a cloud CLI tool.

    Args:
        scheme: Cloud scheme

    Returns:
        Installation hint string
    """
    return f"Unknown cloud scheme: {scheme}"
