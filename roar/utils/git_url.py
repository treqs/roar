"""
Git URL utilities for SSH to HTTPS conversion and URL normalization.

Supports converting SSH URLs to HTTPS equivalents for fallback cloning,
and normalizing URLs for comparison across protocols.
"""

import re


def normalize_git_url(url: str) -> str:
    """
    Normalize a git URL to a canonical form for comparison.

    Strips protocol, authentication, and .git suffix so that equivalent
    URLs can be compared regardless of access method.

    Examples:
        git@github.com:user/repo.git   -> github.com/user/repo
        https://github.com/user/repo.git -> github.com/user/repo
        ssh://git@github.com/user/repo   -> github.com/user/repo

    Args:
        url: Git repository URL

    Returns:
        Normalized URL string (host/path without protocol or .git suffix)
    """
    # SCP format: git@host:path
    scp_match = re.match(r"^git@([^:/]+):(.+)$", url)
    if scp_match:
        host, path = scp_match.group(1), scp_match.group(2)
        return f"{host}/{path.removesuffix('.git')}"

    # SSH scheme: ssh://git@host/path
    ssh_match = re.match(r"^ssh://git@([^/]+)/(.+)$", url)
    if ssh_match:
        host, path = ssh_match.group(1), ssh_match.group(2)
        return f"{host}/{path.removesuffix('.git')}"

    # HTTPS/HTTP: https://host/path
    https_match = re.match(r"^https?://([^/]+)/(.+)$", url)
    if https_match:
        host, path = https_match.group(1), https_match.group(2)
        return f"{host}/{path.removesuffix('.git')}"

    # Fallback: return as-is stripped of .git
    return url.removesuffix(".git")


def urls_match(url1: str, url2: str) -> bool:
    """
    Check if two git URLs refer to the same repository.

    Args:
        url1: First git URL
        url2: Second git URL

    Returns:
        True if both URLs normalize to the same value
    """
    return normalize_git_url(url1) == normalize_git_url(url2)


def is_ssh_url(url: str) -> bool:
    """
    Check if URL is SSH format.

    Supports:
    - SCP-like: git@github.com:user/repo.git
    - SSH scheme: ssh://git@github.com/user/repo.git

    Args:
        url: Git repository URL

    Returns:
        True if URL is SSH format, False otherwise
    """
    return bool(re.match(r"^(?:ssh://)?git@([^:/]+)[:/]", url))


def ssh_to_https(ssh_url: str) -> str | None:
    """
    Convert SSH URL to HTTPS equivalent.

    Converts:
    - git@github.com:user/repo.git -> https://github.com/user/repo.git
    - ssh://git@github.com/user/repo.git -> https://github.com/user/repo.git

    Args:
        ssh_url: SSH git URL

    Returns:
        HTTPS URL if conversion successful, None if not an SSH URL
    """
    # SCP format: git@host:path
    scp_match = re.match(r"^git@([^:/]+):(.+)$", ssh_url)
    if scp_match:
        return f"https://{scp_match.group(1)}/{scp_match.group(2)}"

    # SSH scheme: ssh://git@host/path
    ssh_match = re.match(r"^ssh://git@([^/]+)/(.+)$", ssh_url)
    if ssh_match:
        return f"https://{ssh_match.group(1)}/{ssh_match.group(2)}"

    return None
