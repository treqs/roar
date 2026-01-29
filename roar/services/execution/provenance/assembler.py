"""
Assembler service for provenance collection.

Assembles the final provenance output from collected data.
"""

from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import ProvenanceContext, RuntimeInfo


class ProvenanceAssemblerService:
    """Assembles final provenance output from context."""

    # Code file extensions (for unmanaged_code filtering)
    CODE_EXTENSIONS = (".py", ".so", ".pyx", ".pxd", ".c", ".cpp", ".h", ".hpp", ".rs", ".go")

    def __init__(self, logger: ILogger | None = None) -> None:
        """Initialize assembler with optional logger."""
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ....core.container import get_container
            from ....services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def assemble(self, ctx: ProvenanceContext, config: dict[str, Any]) -> dict[str, Any]:
        """
        Assemble final provenance dict from context.

        Args:
            ctx: ProvenanceContext with all collected data
            config: Configuration dict

        Returns:
            Final provenance output dict
        """
        self.logger.debug("ProvenanceAssemblerService.assemble: building final output")

        # Check config for what to include
        track_repo_files = config.get("output", {}).get("track_repo_files", False)
        self.logger.debug("Config: track_repo_files=%s", track_repo_files)

        # Build code section
        code_section = {
            "repo_root": ctx.repo_root,
            "git": ctx.git_info,
        }
        if track_repo_files:
            code_section["files"] = ctx.classification.get("repo_files", [])

        # Filter unmanaged to only actual code files
        unmanaged = ctx.classification.get("unmanaged", [])
        filtered_unmanaged = [f for f in unmanaged if not self._is_unmanaged_noise(f)]
        self.logger.debug(
            "Unmanaged code filtering: %d -> %d files", len(unmanaged), len(filtered_unmanaged)
        )

        # Filter read_files
        read_files = ctx.filtered_files.read_files
        filtered_read_files = [f for f in read_files if not self._is_read_noise(f)]
        # Remove only code files in the repo (they're in code section)
        # Data files in the repo should remain as they are actual data inputs
        repo_files = set(ctx.classification.get("repo_files", []))
        repo_code_files = {f for f in repo_files if self._is_code_file(f)}
        filtered_read_files = sorted(set(filtered_read_files) - repo_code_files)
        self.logger.debug(
            "Read files filtering: %d -> %d files (removed %d repo code files)",
            len(read_files),
            len(filtered_read_files),
            len(repo_code_files),
        )

        # Build result
        result = {
            "executables": {
                "code": code_section,
                "packages": ctx.packages,
                "unmanaged_code": filtered_unmanaged,
            },
            "data": {
                "read_files": filtered_read_files,
                "written_files": sorted(ctx.filtered_files.written_files),
            },
            "processes": ctx.process_summary,
            "runtime": self._runtime_to_dict(ctx.runtime_info),
        }

        # Add analyzer results
        if ctx.analyzer_results:
            result["analysis"] = ctx.analyzer_results
            self.logger.debug("Added %d analyzer results", len(ctx.analyzer_results))

        self.logger.debug(
            "Assembly complete: read_files=%d, written_files=%d, packages=%d",
            len(filtered_read_files),
            len(ctx.filtered_files.written_files),
            len(ctx.packages),
        )
        return result

    def _runtime_to_dict(self, runtime: RuntimeInfo) -> dict[str, Any]:
        """
        Convert RuntimeInfo dataclass to dict, omitting None values.

        Args:
            runtime: RuntimeInfo instance

        Returns:
            Dict representation with None values omitted
        """
        result: dict[str, Any] = {
            "hostname": runtime.hostname,
            "timing": runtime.timing,
            "command": runtime.command,
            "os": runtime.os,
            "python": runtime.python,
            "env_vars": runtime.env_vars,
        }

        # Add optional fields only if present
        if runtime.container:
            result["container"] = runtime.container
        if runtime.vm:
            result["vm"] = runtime.vm
        if runtime.cuda:
            result["cuda"] = runtime.cuda
        if runtime.gpu:
            result["gpu"] = runtime.gpu
        if runtime.cpu:
            result["cpu"] = runtime.cpu
        if runtime.memory:
            result["memory"] = runtime.memory

        return result

    def _is_code_file(self, path: str) -> bool:
        """Check if path is a code file (not data)."""
        # Check extension
        for ext in self.CODE_EXTENSIONS:
            if path.endswith(ext):
                return True
        # .so files with version suffixes
        return bool(".so." in path or path.endswith(".so"))

    def _is_unmanaged_noise(self, path: str) -> bool:
        """Filter out noise from unmanaged_code list."""
        # .pyc files are derived from .py files, not independent code
        if path.endswith(".pyc"):
            return True
        # roar's own files (including log files in .roar/)
        if ".roar" in path:
            return True
        # triton/torch cache
        if ".triton" in path or "torchinductor" in path:
            return True
        # Data files should be in data.read_files, not unmanaged_code
        return bool(not self._is_code_file(path))

    def _is_read_noise(self, path: str) -> bool:
        """Filter noise from read_files."""
        if path.endswith(".pyc"):
            return True
        if ".triton" in path:
            return True
        if "roar/roar/inject" in path:
            return True
        # System libraries (covered by dpkg packages)
        return bool(path.startswith(("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/")))
