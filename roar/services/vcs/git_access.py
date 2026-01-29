"""
Git access service for checking push permissions.

Extracted from put.py to follow Single Responsibility Principle.
"""

import re
import subprocess
from dataclasses import dataclass


@dataclass
class AccessCheckResult:
    """Result of a git access check."""

    has_access: bool
    error: str | None = None


class GitAccessService:
    """
    Service for checking git push access permissions.

    This service validates that the user has write access to a git
    repository, which is required for reproducibility tagging.

    Usage:
        service = GitAccessService()
        result = service.check_push_access(git_url, repo_root)
        if not result.has_access:
            print(f"No access: {result.error}")
    """

    def check_push_access(
        self,
        git_url: str,
        repo_root: str | None = None,
        timeout: int = 30,
    ) -> AccessCheckResult:
        """
        Check if we have push access to the git remote.

        Attempts to verify push access via:
        1. `git push --dry-run` if repo_root is provided
        2. SSH connectivity test for SSH URLs
        3. Assumes access for HTTPS URLs (can't easily test)

        Args:
            git_url: Git remote URL (SSH or HTTPS)
            repo_root: Local repository root path
            timeout: Timeout in seconds for operations

        Returns:
            AccessCheckResult indicating whether access is available
        """
        if not git_url:
            return AccessCheckResult(has_access=False, error="No git URL")

        # Try git push --dry-run if we have repo root
        if repo_root:
            result = self._try_dry_run_push(repo_root, timeout)
            if result is not None:
                return result

        # Fallback: Parse SSH URL and test basic SSH connectivity
        ssh_result = self._try_ssh_connectivity(git_url, timeout)
        if ssh_result is not None:
            return ssh_result

        # HTTPS URL - can't easily test, assume it will work
        if git_url.startswith("https://"):
            return AccessCheckResult(has_access=True)

        # Unknown URL type, assume it will work
        return AccessCheckResult(has_access=True)

    def _try_dry_run_push(
        self,
        repo_root: str,
        timeout: int,
    ) -> AccessCheckResult | None:
        """
        Try git push --dry-run to check access.

        Returns None if we should fall back to other methods.
        """
        try:
            result = subprocess.run(
                ["git", "push", "--dry-run", "origin", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                return AccessCheckResult(has_access=True)

            stderr = result.stderr.lower()
            if "permission denied" in stderr:
                return AccessCheckResult(
                    has_access=False,
                    error="Permission denied (no push access to repository)",
                )
            if "could not read from remote" in stderr:
                return AccessCheckResult(
                    has_access=False,
                    error="Cannot access remote repository (check SSH key/permissions)",
                )
            if "authentication failed" in stderr:
                return AccessCheckResult(
                    has_access=False,
                    error="Authentication failed",
                )

            return AccessCheckResult(has_access=False, error=result.stderr.strip())

        except subprocess.TimeoutExpired:
            return AccessCheckResult(
                has_access=False,
                error="Git push check timed out",
            )
        except Exception:
            # Fall through to other methods
            return None

    def _try_ssh_connectivity(
        self,
        git_url: str,
        timeout: int,
    ) -> AccessCheckResult | None:
        """
        Try SSH connectivity test for SSH URLs.

        Returns None if URL is not SSH format.
        """
        ssh_match = re.match(r"^(?:ssh://)?git@([^:/]+)[:/]", git_url)
        if not ssh_match:
            return None

        host = ssh_match.group(1)
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    f"git@{host}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout // 3,  # Shorter timeout for SSH test
            )

            if result.returncode == 255:
                return AccessCheckResult(
                    has_access=False,
                    error=f"SSH access denied to {host}",
                )
            if "Permission denied" in result.stderr:
                return AccessCheckResult(
                    has_access=False,
                    error=f"SSH access denied to {host}",
                )

            return AccessCheckResult(has_access=True)

        except subprocess.TimeoutExpired:
            return AccessCheckResult(
                has_access=False,
                error=f"SSH connection to {host} timed out",
            )
        except Exception as e:
            return AccessCheckResult(has_access=False, error=str(e))

    def check_branch_pushed(self, repo_root: str) -> tuple[bool, str | None]:
        """
        Check if the current branch is pushed to remote.

        Args:
            repo_root: Local repository root path

        Returns:
            Tuple of (is_pushed, error_message)
        """
        try:
            result = subprocess.run(
                ["git", "branch", "-r", "--contains", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return False, "Could not check branch status"

            if not result.stdout.strip():
                return False, "Current commit hasn't been pushed to remote"

            return True, None

        except Exception as e:
            return False, str(e)
