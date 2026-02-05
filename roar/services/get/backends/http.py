"""
HTTP/HTTPS download backend.

Downloads artifacts from HTTP/HTTPS URLs using urllib (no external deps).
"""

from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .base import DownloadBackend

# Default buffer size for streaming downloads
_CHUNK_SIZE = 8192

# User-Agent header for requests
_USER_AGENT = "roar/1.0"


class HTTPBackend(DownloadBackend):
    """
    HTTP/HTTPS download backend.

    Uses urllib for streaming downloads. Supports:
    - Content-Disposition header for filename extraction
    - Redirect following (automatic with urllib)
    - HEAD requests for existence checks
    - No external dependencies
    """

    def __init__(self, url: str):
        """
        Initialize HTTP backend.

        Args:
            url: The full HTTP/HTTPS URL to download from.
        """
        self._url = url

    def download(self, remote_key: str, local_path: Path) -> None:
        """
        Download a file from an HTTP(S) URL.

        The remote_key is ignored for HTTP — the full URL from __init__ is used.

        Args:
            remote_key: Ignored (URL set at init time).
            local_path: Local path to save the file to.

        Raises:
            FileNotFoundError: If the URL returns 404.
            IOError: If download fails.
        """
        from urllib.error import HTTPError

        req = Request(self._url, headers={"User-Agent": _USER_AGENT})

        try:
            with urlopen(req) as response:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    while True:
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
        except HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(f"URL not found: {self._url}") from e
            raise OSError(f"HTTP error {e.code}: {self._url}") from e

    def exists(self, remote_key: str) -> bool:
        """
        Check if the URL exists via HEAD request.

        Args:
            remote_key: Ignored (URL set at init time).

        Returns:
            True if the URL returns a successful status code.
        """
        from urllib.error import HTTPError, URLError

        req = Request(
            self._url,
            method="HEAD",
            headers={"User-Agent": _USER_AGENT},
        )
        try:
            with urlopen(req) as response:
                return response.status < 400
        except HTTPError:
            return False
        except URLError:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        """
        HTTP doesn't support prefix listing.

        Returns a single-element list with the URL path as the key.
        """
        # HTTP only supports single file downloads
        parsed = urlparse(self._url)
        return [parsed.path.lstrip("/")]

    def get_filename_from_headers(self) -> str | None:
        """
        Get filename from Content-Disposition header if available.

        Returns:
            Filename from headers, or None if not available.
        """
        from urllib.error import URLError

        req = Request(
            self._url,
            method="HEAD",
            headers={"User-Agent": _USER_AGENT},
        )
        try:
            with urlopen(req) as response:
                cd = response.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    # Parse filename from Content-Disposition
                    for part in cd.split(";"):
                        part = part.strip()
                        if part.startswith("filename="):
                            filename = part[len("filename=") :]
                            # Remove quotes if present
                            filename = filename.strip('"').strip("'")
                            return unquote(filename)
        except (URLError, OSError):
            pass
        return None
