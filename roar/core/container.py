"""
Dependency injection container for roar.

Uses dependency-injector for DI with support for:
- Singleton and transient lifetimes
- Factory registration
- Interface-based resolution
- Plugin registries for extensible components
"""

from collections.abc import Callable
from typing import Optional, TypeVar

from dependency_injector import providers

from .interfaces.cloud import ICloudStorageProvider
from .interfaces.command import ICommand
from .interfaces.telemetry import ITelemetryProvider
from .interfaces.vcs import IVCSProvider

T = TypeVar("T")


class ServiceContainer:
    """
    Dependency injection container for roar.

    Combines dependency-injector's DI capabilities with plugin registries
    for extensible components like cloud providers and analyzers.
    """

    _instance: Optional["ServiceContainer"] = None

    def __init__(self) -> None:
        """Initialize the container with empty registries."""
        # Dynamic provider storage (interface -> provider)
        self._providers: dict[type, providers.Provider] = {}

        # Plugin registries (multiple implementations per interface)
        self._cloud_providers: dict[str, type[ICloudStorageProvider]] = {}
        self._telemetry_providers: dict[str, type[ITelemetryProvider]] = {}
        self._vcs_providers: dict[str, type[IVCSProvider]] = {}
        self._commands: dict[str, type[ICommand]] = {}
        self._command_aliases: dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> "ServiceContainer":
        """Get the global container instance (singleton)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the global container (for testing)."""
        cls._instance = None

    # -------------------------------------------------------------------------
    # Core service registration (uses dependency-injector providers)
    # -------------------------------------------------------------------------

    def register_singleton(
        self,
        interface: type[T],
        implementation: T | None = None,
        factory: Callable[[], T] | None = None,
    ) -> None:
        """
        Register a singleton service.

        Args:
            interface: The interface/protocol type
            implementation: Optional concrete instance
            factory: Optional factory function (for lazy init)
        """
        if implementation is not None:
            # Use Object provider for pre-created instances
            self._providers[interface] = providers.Object(implementation)
        elif factory is not None:
            # Use Singleton provider with factory for lazy initialization
            self._providers[interface] = providers.Singleton(factory)
        else:
            raise ValueError("Must provide either implementation or factory")

    def register_transient(
        self,
        interface: type[T],
        factory: Callable[..., T],
    ) -> None:
        """
        Register a transient service (new instance per resolve).

        Args:
            interface: The interface/protocol type
            factory: Factory function or class
        """
        self._providers[interface] = providers.Factory(factory)

    def register_class(
        self,
        interface: type[T],
        implementation: type[T],
        scope: str = "singleton",
    ) -> None:
        """
        Register a class implementation.

        Args:
            interface: The interface/protocol type
            implementation: Concrete class type
            scope: 'singleton' or 'transient'
        """
        if scope == "singleton":
            self._providers[interface] = providers.Singleton(implementation)
        else:
            self._providers[interface] = providers.Factory(implementation)

    def resolve(self, interface: type[T]) -> T:
        """
        Resolve a service by interface.

        Args:
            interface: The interface/protocol type to resolve

        Returns:
            The registered implementation

        Raises:
            KeyError: If no registration found
        """
        if interface not in self._providers:
            raise KeyError(f"No provider registered for: {interface}")
        return self._providers[interface]()

    def try_resolve(self, interface: type[T]) -> T | None:
        """
        Try to resolve a service, returning None if not registered.

        Args:
            interface: The interface/protocol type to resolve

        Returns:
            The registered implementation, or None
        """
        if interface not in self._providers:
            return None
        return self._providers[interface]()

    def override(self, interface: type[T], provider: providers.Provider) -> None:
        """
        Override a registered provider (useful for testing).

        Args:
            interface: The interface to override
            provider: The new provider to use
        """
        self._providers[interface] = provider

    # -------------------------------------------------------------------------
    # Cloud provider registry
    # -------------------------------------------------------------------------

    def register_cloud_provider(
        self,
        scheme: str,
        provider_class: type[ICloudStorageProvider],
    ) -> None:
        """
        Register a cloud storage provider.

        Args:
            scheme: URL scheme (e.g., 's3', 'gs', 'az')
            provider_class: Provider class implementing ICloudStorageProvider
        """
        self._cloud_providers[scheme] = provider_class

    def get_cloud_provider(self, scheme: str) -> ICloudStorageProvider:
        """
        Get a cloud provider instance by scheme.

        Args:
            scheme: URL scheme

        Returns:
            Provider instance

        Raises:
            KeyError: If no provider registered for scheme
        """
        if scheme not in self._cloud_providers:
            raise KeyError(f"No cloud provider registered for scheme: {scheme}")
        return self._cloud_providers[scheme]()

    def list_cloud_providers(self) -> list[str]:
        """List registered cloud provider schemes."""
        return list(self._cloud_providers.keys())

    # -------------------------------------------------------------------------
    # Telemetry provider registry
    # -------------------------------------------------------------------------

    def register_telemetry_provider(
        self,
        name: str,
        provider_class: type[ITelemetryProvider],
    ) -> None:
        """
        Register a telemetry provider.

        Args:
            name: Provider name (e.g., 'wandb', 'mlflow')
            provider_class: Provider class implementing ITelemetryProvider
        """
        self._telemetry_providers[name] = provider_class

    def get_telemetry_provider(self, name: str) -> ITelemetryProvider:
        """
        Get a telemetry provider instance by name.

        Args:
            name: Provider name

        Returns:
            Provider instance

        Raises:
            KeyError: If no provider registered
        """
        if name not in self._telemetry_providers:
            raise KeyError(f"No telemetry provider registered: {name}")
        return self._telemetry_providers[name]()

    def get_all_telemetry_providers(self) -> dict[str, ITelemetryProvider]:
        """Get instances of all registered telemetry providers."""
        return {name: cls() for name, cls in self._telemetry_providers.items()}

    def list_telemetry_providers(self) -> list[str]:
        """List registered telemetry provider names."""
        return list(self._telemetry_providers.keys())

    # -------------------------------------------------------------------------
    # VCS provider registry
    # -------------------------------------------------------------------------

    def register_vcs_provider(
        self,
        name: str,
        provider_class: type[IVCSProvider],
    ) -> None:
        """
        Register a VCS provider.

        Args:
            name: Provider name (e.g., 'git', 'hg')
            provider_class: Provider class implementing IVCSProvider
        """
        self._vcs_providers[name] = provider_class

    def get_vcs_provider(self, name: str = "git") -> IVCSProvider:
        """
        Get a VCS provider instance.

        Args:
            name: Provider name (default: 'git')

        Returns:
            Provider instance

        Raises:
            KeyError: If no provider registered
        """
        if name not in self._vcs_providers:
            raise KeyError(f"No VCS provider registered: {name}")
        return self._vcs_providers[name]()

    def list_vcs_providers(self) -> list[str]:
        """List registered VCS provider names."""
        return list(self._vcs_providers.keys())

    # -------------------------------------------------------------------------
    # Command registry
    # -------------------------------------------------------------------------

    def register_command(
        self,
        command_class: type[ICommand],
    ) -> type[ICommand]:
        """
        Register a command class.

        Can be used as a decorator:
            @container.register_command
            class StatusCommand(ICommand):
                ...

        Args:
            command_class: Command class implementing ICommand

        Returns:
            The command class (for decorator use)
        """
        # Create a temporary instance to get name and aliases
        # This works because commands shouldn't have required constructor args
        try:
            instance = command_class.__new__(command_class)
            name = instance.name
            aliases = getattr(instance, "aliases", [])
        except (TypeError, AttributeError):
            # Fallback: try to get from class attributes
            name = getattr(command_class, "name", command_class.__name__.lower())
            aliases = getattr(command_class, "aliases", [])

        self._commands[name] = command_class
        for alias in aliases:
            self._command_aliases[alias] = name

        return command_class

    def get_command(self, name: str) -> type[ICommand] | None:
        """
        Get a command class by name or alias.

        Args:
            name: Command name or alias

        Returns:
            Command class, or None if not found
        """
        # Check aliases first
        if name in self._command_aliases:
            name = self._command_aliases[name]
        return self._commands.get(name)

    def list_commands(self) -> dict[str, type[ICommand]]:
        """Get all registered commands."""
        return dict(self._commands)

    def get_command_help_text(self) -> str:
        """Generate help text for all commands."""
        lines = []
        for name, cmd_class in sorted(self._commands.items()):
            help_text = getattr(cmd_class, "help_text", "")
            if not help_text:
                try:
                    instance = cmd_class.__new__(cmd_class)
                    help_text = instance.help_text
                except (TypeError, AttributeError):
                    pass
            lines.append(f"  {name:18} {help_text}")
        return "\n".join(lines)


# -------------------------------------------------------------------------
# Module-level convenience functions
# -------------------------------------------------------------------------


def get_container() -> ServiceContainer:
    """Get the global service container instance."""
    return ServiceContainer.get_instance()


def resolve(interface: type[T]) -> T:
    """Resolve a service from the global container."""
    return get_container().resolve(interface)


def try_resolve(interface: type[T]) -> T | None:
    """Try to resolve a service, returning None if not registered."""
    return get_container().try_resolve(interface)
