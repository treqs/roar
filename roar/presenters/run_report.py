"""
Run report presenter for displaying run completion reports.

Handles all output formatting for run/build results.
Follows SRP: only handles report presentation.
"""

import os
import shlex
from typing import Any

from ..core.interfaces.presenter import IPresenter
from ..core.interfaces.run import RunResult


def format_size(size_bytes: int | None) -> str:
    """Format file size in human-readable format."""
    if size_bytes is None:
        return "?"
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


class RunReportPresenter:
    """
    Formats and displays run completion reports.

    Follows SRP: only handles report presentation.
    """

    def __init__(self, presenter: IPresenter) -> None:
        """
        Initialize report presenter.

        Args:
            presenter: Base presenter for output
        """
        self._out = presenter

    def show_report(
        self,
        result: RunResult,
        command: list[str],
        quiet: bool = False,
    ) -> None:
        """
        Display run completion report.

        Args:
            result: Run result with execution details
            command: Original command that was executed
            quiet: If True, suppress output
        """
        if quiet:
            return

        self._out.print("")
        self._out.print("=" * 60)

        step_type = "Build" if result.is_build else "Run"
        if result.interrupted:
            self._out.print(f"ROAR {step_type} Interrupted")
        else:
            self._out.print(f"ROAR {step_type} Complete")

        self._out.print("=" * 60)
        self._out.print(f"Command: {shlex.join(command)}")
        self._out.print(f"Duration: {result.duration:.1f}s")
        self._out.print(f"Exit code: {result.exit_code}")

        if result.interrupted:
            self._out.print("Status: interrupted")

        self._out.print("")

        if result.inputs:
            self._out.print("Read files:")
            for f in result.inputs:
                self._print_file(f)
            self._out.print("")

        if result.outputs:
            self._out.print("Written files:")
            for f in result.outputs:
                self._print_file(f)
            self._out.print("")

        self._out.print(f"Job: {result.job_uid}")

        if result.interrupted and result.outputs:
            self._out.print("")
            self._out.print("Note: Run was interrupted. Output files may be incomplete.")
            self._out.print("Use 'roar clean' to remove written files if needed.")

    def show_stale_warnings(
        self,
        stale_upstream: list[int],
        stale_downstream: list[int],
        is_build: bool = False,
    ) -> None:
        """
        Display stale step warnings.

        Args:
            stale_upstream: List of stale upstream step numbers
            stale_downstream: List of stale downstream step numbers
            is_build: Whether this is a build step
        """
        if stale_upstream:
            self._out.print("")
            step_refs = ", ".join(f"@{s}" for s in stale_upstream)
            self._out.print(f"Warning: This job consumed stale inputs from: {step_refs}")
            self._out.print("The upstream steps were re-run but this step used old outputs.")
            self._out.print("Consider re-running this step after updating upstream.")

        if stale_downstream:
            self._out.print("")
            step_prefix = "B" if is_build else ""
            step_refs = ", ".join(f"@{step_prefix}{s}" for s in stale_downstream)
            self._out.print(f"Warning: Downstream steps are stale: {step_refs}")
            self._out.print("Run these steps to update them, or use 'roar dag' to see full status.")

    def show_upstream_stale_warning(
        self,
        step_num: int,
        upstream_stale: list[int],
    ) -> bool:
        """
        Show warning about stale upstream steps and ask for confirmation.

        Args:
            step_num: Current step number
            upstream_stale: List of stale upstream step numbers

        Returns:
            True if user wants to proceed, False otherwise
        """
        step_refs = ", ".join(f"@{s}" for s in upstream_stale)
        self._out.print(f"Warning: Step @{step_num} depends on stale upstream steps: {step_refs}")
        self._out.print(
            "The upstream steps have been re-run more recently than their outputs were consumed."
        )
        self._out.print("")

        return self._out.confirm("Run anyway?", default=False)

    def _print_file(self, f: dict[str, Any]) -> None:
        """Print file info with path, size, and hashes."""
        path = f["path"]
        size = format_size(f.get("size"))

        # Make path relative if possible
        try:
            rel_path = os.path.relpath(path)
            if not rel_path.startswith(".."):
                path = rel_path
        except ValueError:
            pass

        self._out.print(f"  {path}")

        # Show all hashes
        hashes = f.get("hashes", [])
        if hashes:
            hash_strs = [f"{h['algorithm']}: {h['digest'][:12]}..." for h in hashes]
            self._out.print(f"    size: {size}  {', '.join(hash_strs)}")
        else:
            self._out.print(f"    size: {size}")
