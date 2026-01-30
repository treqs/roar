"""
Secret filter service.

Wraps OmitFilter with the ISecretFilter protocol for use in registration services.
"""

from typing import Any

from ...config import config_get
from ...core.interfaces.registration import ISecretFilter
from ...filters.omit import OmitFilter


class SecretFilterService(ISecretFilter):
    """
    Service for filtering secrets from registration data.

    Wraps OmitFilter to provide consistent interface for registration services.
    Consolidates the duplicated OmitFilter initialization from:
    - put.py:124-125
    - RegisterService
    """

    def __init__(self, omit_filter: OmitFilter | None = None):
        """
        Initialize the secret filter service.

        Args:
            omit_filter: Pre-configured OmitFilter, or None to disable filtering.
        """
        self._filter = omit_filter

    @classmethod
    def from_config(cls, config_key: str = "registration.omit") -> "SecretFilterService":
        """
        Create filter service from configuration.

        Consolidates the duplicated config lookup pattern:
        ```python
        omit_config = config_get("registration.omit")
        omit_filter = OmitFilter(omit_config) if omit_config else None
        ```

        Args:
            config_key: Configuration key for omit settings

        Returns:
            SecretFilterService with configured filter, or disabled filter if no config
        """
        omit_config = config_get(config_key)
        if omit_config:
            return cls(OmitFilter(omit_config))
        return cls(None)

    @classmethod
    def from_dict(cls, config: dict[str, Any] | None) -> "SecretFilterService":
        """
        Create filter service from config dict.

        Args:
            config: Configuration dict for OmitFilter

        Returns:
            SecretFilterService with configured filter
        """
        if config:
            return cls(OmitFilter(config))
        return cls(None)

    @property
    def enabled(self) -> bool:
        """Check if filtering is enabled."""
        return self._filter is not None and self._filter.enabled

    def filter_command(self, command: str) -> tuple[str, list[str]]:
        """
        Filter secrets from command string.

        Args:
            command: Raw command string

        Returns:
            Tuple of (filtered_command, list_of_detection_ids)
        """
        if not self._filter:
            return command, []

        return self._filter.filter_command(command)

    def filter_git_url(self, url: str) -> tuple[str, list[str]]:
        """
        Filter credentials from git URL.

        Args:
            url: Raw git URL

        Returns:
            Tuple of (filtered_url, list_of_detection_ids)
        """
        if not self._filter:
            return url, []

        return self._filter.filter_git_url(url)

    def filter_metadata(self, metadata: dict) -> tuple[dict, list[str]]:
        """
        Filter secrets from metadata dict.

        Args:
            metadata: Raw metadata dict

        Returns:
            Tuple of (filtered_metadata, list_of_detection_ids)
        """
        if not self._filter:
            return metadata, []

        return self._filter.filter_metadata(metadata)

    def filter_telemetry(self, telemetry: str) -> tuple[str, list[str]]:
        """
        Filter secrets from telemetry JSON string.

        Args:
            telemetry: Raw telemetry JSON string

        Returns:
            Tuple of (filtered_telemetry, list_of_detection_ids)
        """
        if not self._filter:
            return telemetry, []

        return self._filter.filter_telemetry(telemetry)
