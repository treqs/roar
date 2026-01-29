"""
Command interface definitions for CLI layer.

Defines the contract for command handlers following LSP.
"""

from abc import ABC, abstractmethod

# Re-export models for backward compatibility
from roar.core.models.command import CommandContext, CommandResult


class ICommand(ABC):
    """
    Interface for command handlers.

    All commands must implement this interface to ensure
    consistent behavior (Liskov Substitution Principle).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Command name (e.g., 'status', 'run')."""
        pass

    @property
    def aliases(self) -> list[str]:
        """Command aliases (e.g., ['st'] for status)."""
        return []

    @property
    def help_text(self) -> str:
        """Short description for help listing."""
        return ""

    @abstractmethod
    def execute(self, ctx: CommandContext) -> CommandResult:
        """
        Execute the command.

        Args:
            ctx: Command context with args, paths, and flags

        Returns:
            CommandResult with success status and exit code

        Note:
            Commands should NOT call sys.exit(). Return CommandResult instead.
            Commands should raise RoarError for recoverable errors.
        """
        pass

    def validate_args(self, ctx: CommandContext) -> str | None:
        """
        Validate arguments before execution.

        Args:
            ctx: Command context

        Returns:
            Error message if invalid, None if valid
        """
        return None

    def get_help(self) -> str:
        """Return detailed help text."""
        return self.help_text

    def requires_init(self) -> bool:
        """Whether this command requires roar to be initialized."""
        return True

    def requires_git(self) -> bool:
        """Whether this command requires a git repository."""
        return False
