"""Dependency injection container for roar core services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Minimal provider implementations (replaces dependency_injector)
# ---------------------------------------------------------------------------


class _Provider:
    """Base provider protocol."""

    def __call__(self) -> Any:
        raise NotImplementedError


class _ObjectProvider(_Provider):
    """Returns the same pre-created instance every time."""

    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def __call__(self) -> Any:
        return self._obj


class _SingletonProvider(_Provider):
    """Calls factory once, caches and returns the result thereafter."""

    __slots__ = ("_factory", "_instance")

    def __init__(self, factory: Callable[..., Any]) -> None:
        self._factory = factory
        self._instance: Any = _SENTINEL

    def __call__(self) -> Any:
        if self._instance is _SENTINEL:
            self._instance = self._factory()
        return self._instance


class _FactoryProvider(_Provider):
    """Calls factory on every resolution (transient)."""

    __slots__ = ("_factory",)

    def __init__(self, factory: Callable[..., Any]) -> None:
        self._factory = factory

    def __call__(self) -> Any:
        return self._factory()


_SENTINEL = object()


class ServiceContainer:
    """
    Dependency injection container for roar.

    Provides DI capabilities for core application services.
    """

    _instance: ServiceContainer | None = None

    def __init__(self) -> None:
        """Initialize the container with empty providers."""
        # Dynamic provider storage (interface -> provider)
        self._providers: dict[type, _Provider] = {}

    @classmethod
    def get_instance(cls) -> ServiceContainer:
        """Get the global container instance (singleton)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the global container (for testing)."""
        cls._instance = None

    # -------------------------------------------------------------------------
    # Core service registration
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
            self._providers[interface] = _ObjectProvider(implementation)
        elif factory is not None:
            self._providers[interface] = _SingletonProvider(factory)
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
        self._providers[interface] = _FactoryProvider(factory)

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
            self._providers[interface] = _SingletonProvider(implementation)
        else:
            self._providers[interface] = _FactoryProvider(implementation)

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

    def override(self, interface: type[T], provider: _Provider) -> None:
        """
        Override a registered provider (useful for testing).

        Args:
            interface: The interface to override
            provider: The new provider to use
        """
        self._providers[interface] = provider

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
