"""
Configuration provider interface definitions.

Enables injectable configuration access rather than global state.
"""

from abc import ABC, abstractmethod
from typing import Any


class IConfigProvider(ABC):
    """
    Interface for configuration access.

    Implementations provide access to roar configuration values,
    enabling dependency injection rather than global config access.
    """

    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a config value by dot-notation key.

        Args:
            key: Dot-notation key (e.g., 'register.omit.enabled', 'filters.ignore_system_reads')
            default: Default value if key not found

        Returns:
            Config value or default
        """
        pass

    @abstractmethod
    def set(self, key: str, value: Any) -> None:
        """
        Set a config value.

        Args:
            key: Dot-notation key
            value: Value to set
        """
        pass

    @abstractmethod
    def get_section(self, section: str) -> dict[str, Any]:
        """
        Get an entire config section.

        Args:
            section: Section name (e.g., 'filters', 'register')

        Returns:
            Dictionary of section values
        """
        pass

    @abstractmethod
    def reload(self) -> None:
        """Reload configuration from disk."""
        pass

    @abstractmethod
    def list_keys(self) -> dict[str, dict[str, Any]]:
        """
        List all available config keys with their metadata.

        Returns:
            Dict mapping key names to {default, description, type}
        """
        pass
