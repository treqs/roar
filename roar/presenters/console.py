"""
Console presenter for terminal output.

Implements human-readable output formatting for the CLI.
"""

import sys
from typing import Any

from ..core.interfaces.presenter import IPresenter


class ConsolePresenter(IPresenter):
    """
    Console output presenter.

    Formats output for human-readable terminal display.
    """

    def __init__(self, use_color: bool = True, file=None) -> None:
        """
        Initialize console presenter.

        Args:
            use_color: Whether to use ANSI color codes
            file: Output file (defaults to sys.stdout)
        """
        self._use_color = use_color and sys.stdout.isatty()
        self._file = file or sys.stdout
        self._err_file = sys.stderr

    def print(self, message: str) -> None:
        """Print a message to output."""
        print(message, file=self._file)

    def print_error(self, message: str) -> None:
        """Print an error message to stderr."""
        if self._use_color:
            print(f"\033[91mError: {message}\033[0m", file=self._err_file)
        else:
            print(f"Error: {message}", file=self._err_file)

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        if self._use_color:
            print(f"\033[93mWarning: {message}\033[0m", file=self._err_file)
        else:
            print(f"Warning: {message}", file=self._err_file)

    def print_success(self, message: str) -> None:
        """Print a success message."""
        if self._use_color:
            print(f"\033[92m{message}\033[0m", file=self._file)
        else:
            print(message, file=self._file)

    def print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        """
        Print a formatted table.

        Args:
            headers: Column headers
            rows: Table rows
        """
        if not rows:
            return

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(cell)))

        # Print header
        header_line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
        if self._use_color:
            print(f"\033[1m{header_line}\033[0m", file=self._file)
        else:
            print(header_line, file=self._file)

        # Print separator
        print("-" * len(header_line), file=self._file)

        # Print rows
        for row in rows:
            row_line = "  ".join(
                str(cell).ljust(widths[i]) if i < len(widths) else str(cell)
                for i, cell in enumerate(row)
            )
            print(row_line, file=self._file)

    def print_job(self, job: dict[str, Any], verbose: bool = False) -> None:
        """
        Print job details.

        Args:
            job: Job dictionary with keys like 'id', 'command', 'started', etc.
            verbose: Whether to show extended details
        """
        job_id = job.get("id", "?")
        command = job.get("command", "?")
        started = job.get("started", "?")
        duration = job.get("duration")
        exit_code = job.get("exit_code", "?")

        # Format duration
        if duration is not None:
            if duration < 60:
                dur_str = f"{duration:.1f}s"
            elif duration < 3600:
                dur_str = f"{duration / 60:.1f}m"
            else:
                dur_str = f"{duration / 3600:.1f}h"
        else:
            dur_str = "?"

        # Status indicator
        if exit_code == 0:
            status = "✓" if self._use_color else "[OK]"
            if self._use_color:
                status = f"\033[92m{status}\033[0m"
        else:
            status = "✗" if self._use_color else "[FAIL]"
            if self._use_color:
                status = f"\033[91m{status}\033[0m"

        print(f"{status} [{job_id[:8]}] {command}", file=self._file)

        if verbose:
            print(f"    Started: {started}", file=self._file)
            print(f"    Duration: {dur_str}", file=self._file)
            if job.get("inputs"):
                print(f"    Inputs: {len(job['inputs'])} files", file=self._file)
            if job.get("outputs"):
                print(f"    Outputs: {len(job['outputs'])} files", file=self._file)

    def print_artifact(self, artifact: dict[str, Any]) -> None:
        """
        Print artifact details.

        Args:
            artifact: Artifact dictionary
        """
        hash_val = artifact.get("hash", "?")[:12]
        path = artifact.get("path", "?")
        size = artifact.get("size")

        size_str = format_size(size) if size else "?"

        print(f"  {hash_val}  {size_str:>10}  {path}", file=self._file)

    def print_dag(
        self,
        summary: dict[str, Any],
        stale_steps: set[int] | None = None,
    ) -> None:
        """
        Print DAG summary.

        Args:
            summary: Pipeline summary with 'steps' list
            stale_steps: Set of stale step numbers
        """
        stale_steps = stale_steps or set()
        steps = summary.get("steps", [])

        if not steps:
            print("No steps in pipeline.", file=self._file)
            return

        print(f"Pipeline: {len(steps)} steps", file=self._file)
        print("", file=self._file)

        for i, step in enumerate(steps, 1):
            command = step.get("command", "?")
            is_stale = i in stale_steps

            if is_stale:
                marker = "*" if not self._use_color else "\033[93m*\033[0m"
                print(f"  {i}. {marker} {command}", file=self._file)
            else:
                print(f"  {i}.   {command}", file=self._file)

        if stale_steps:
            print("", file=self._file)
            print(f"* = stale ({len(stale_steps)} steps need re-execution)", file=self._file)

    def confirm(self, message: str, default: bool = False) -> bool:
        """
        Ask for user confirmation.

        Args:
            message: Confirmation prompt
            default: Default value if user presses Enter

        Returns:
            True if confirmed, False otherwise
        """
        suffix = " [Y/n] " if default else " [y/N] "

        try:
            response = input(message + suffix).strip().lower()
            if not response:
                return default
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("", file=self._file)
            return False

    def print_key_value(self, key: str, value: Any, indent: int = 0) -> None:
        """Print a key-value pair."""
        prefix = "  " * indent
        if self._use_color:
            print(f"{prefix}\033[1m{key}:\033[0m {value}", file=self._file)
        else:
            print(f"{prefix}{key}: {value}", file=self._file)

    def print_section(self, title: str) -> None:
        """Print a section header."""
        if self._use_color:
            print(f"\n\033[1m{title}\033[0m", file=self._file)
        else:
            print(f"\n{title}", file=self._file)
        print("-" * len(title), file=self._file)


def format_size(size_bytes: int | None) -> str:
    """Format byte size as human-readable string."""
    if size_bytes is None:
        return "?"

    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"
