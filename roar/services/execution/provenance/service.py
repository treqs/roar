"""
Provenance service orchestrator.

Coordinates all provenance collection services to produce the final output.
"""

from datetime import datetime, timezone
from typing import Any

from .... import analyzers
from ....core.container import get_container
from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import (
    IDataLoader,
    IFileFilterService,
    IPackageCollector,
    IProcessSummarizer,
    IProvenanceAssembler,
    IRuntimeCollector,
    ProvenanceContext,
)
from ....filters import FileClassifier
from .assembler import ProvenanceAssemblerService
from .build_pip_collector import BuildPipCollectorService
from .build_tool_collector import BuildToolCollectorService
from .data_loader import DataLoaderService
from .file_filter import FileFilterService
from .package_collector import PackageCollectorService
from .process_summarizer import ProcessSummarizerService
from .runtime_collector import RuntimeCollectorService


class ProvenanceService:
    """
    Main orchestrator for provenance collection.

    Coordinates:
    - Loading tracer and Python-specific data
    - Running filters to classify files
    - Running analyzers for hygiene checks
    - Assembling the final provenance output
    """

    def __init__(
        self,
        data_loader: IDataLoader | None = None,
        file_filter: IFileFilterService | None = None,
        runtime_collector: IRuntimeCollector | None = None,
        process_summarizer: IProcessSummarizer | None = None,
        package_collector: IPackageCollector | None = None,
        assembler: IProvenanceAssembler | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the provenance service with optional dependencies.

        Args:
            data_loader: Service for loading JSON data (default: DataLoaderService)
            file_filter: Service for filtering files (default: FileFilterService)
            runtime_collector: Service for runtime info (default: RuntimeCollectorService)
            process_summarizer: Service for process tree (default: ProcessSummarizerService)
            package_collector: Service for packages (default: PackageCollectorService)
            assembler: Service for output assembly (default: ProvenanceAssemblerService)
            logger: Logger for internal diagnostics
        """
        self._data_loader = data_loader or DataLoaderService()
        self._file_filter = file_filter or FileFilterService()
        self._runtime_collector = runtime_collector or RuntimeCollectorService()
        self._process_summarizer = process_summarizer or ProcessSummarizerService()
        self._package_collector = package_collector or PackageCollectorService()
        self._assembler = assembler or ProvenanceAssemblerService()
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ....services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def collect(
        self,
        repo_root: str,
        tracer_log_path: str,
        python_log_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Collect provenance from tracer output and optional Python-specific log.

        Args:
            repo_root: Path to the git repository root
            tracer_log_path: Path to the Rust tracer's JSON output
            python_log_path: Optional path to Python sitecustomize.py output
            config: Optional configuration dict (from .roar.toml)

        Returns:
            Complete provenance dict
        """
        config = config or {}
        self.logger.debug(
            "ProvenanceService.collect: repo_root=%s, tracer_log=%s", repo_root, tracer_log_path
        )

        # 1. Load data
        self.logger.debug("Loading tracer data")
        tracer_data = self._data_loader.load_tracer_data(tracer_log_path)
        self.logger.debug(
            "Tracer data loaded: opened=%d, read=%d, written=%d, processes=%d",
            len(tracer_data.opened_files),
            len(tracer_data.read_files),
            len(tracer_data.written_files),
            len(tracer_data.processes),
        )
        python_data = self._data_loader.load_python_data(python_log_path)
        self.logger.debug(
            "Python data loaded: modules=%d, packages=%d",
            len(python_data.modules_files),
            len(python_data.used_packages),
        )

        # 2. Filter files
        self.logger.debug("Filtering files")
        filtered_files = self._file_filter.filter_files(tracer_data, python_data, config)
        self.logger.debug(
            "Files filtered: read=%d, written=%d, opened=%d",
            len(filtered_files.read_files),
            len(filtered_files.written_files),
            len(filtered_files.opened_files),
        )

        # 3. Build timing info
        timing = self._build_timing(tracer_data.start_time, tracer_data.end_time)

        # 4. Collect runtime info
        self.logger.debug("Collecting runtime info")
        runtime_info = self._runtime_collector.collect(python_data, tracer_data, timing)

        # 5. Summarize processes
        self.logger.debug("Summarizing %d processes", len(tracer_data.processes))
        process_info = self._build_process_info(tracer_data.processes)
        process_summary = self._process_summarizer.summarize(process_info)

        # 6. Classify files (via FileClassifier)
        self.logger.debug("Classifying files")
        all_files = list(
            set(
                filtered_files.opened_files
                + filtered_files.read_files
                + filtered_files.modules_files
            )
        )
        classifier = FileClassifier(
            repo_root=repo_root,
            sys_prefix=python_data.sys_prefix,
            sys_base_prefix=python_data.sys_base_prefix,
            roar_inject_dir=python_data.roar_inject_dir,
        )
        classification = classifier.classify_all(all_files)
        self.logger.debug(
            "Classification complete: repo_files=%d, unmanaged=%d",
            len(classification.get("repo_files", [])),
            len(classification.get("unmanaged", [])),
        )

        # 7. Get git info (via VCS provider)
        self.logger.debug("Getting git info")
        git_info = self._get_git_info(repo_root)
        self.logger.debug(
            "Git info: commit=%s, branch=%s",
            git_info.get("commit", "")[:12] if git_info.get("commit") else None,
            git_info.get("branch"),
        )

        # 8. Collect packages
        self.logger.debug("Collecting packages")
        packages: dict[str, dict[str, str | None]] = self._package_collector.collect(  # type: ignore[assignment]
            python_data,
            python_data.shared_libs,
            python_data.sys_prefix,
        )
        # Merge with classification packages if needed
        if not packages.get("pip") and classification.get("packages"):
            packages["pip"] = classification["packages"]
        self.logger.debug(
            "Packages collected: pip=%d, dpkg=%d",
            len(packages.get("pip", {})),
            len(packages.get("dpkg", {})),
        )

        # 8b. Collect build tool dependencies from process tree
        self.logger.debug("Collecting build tool dependencies")
        build_tool_collector = BuildToolCollectorService(logger=self._logger)
        build_dpkg = build_tool_collector.collect(tracer_data.processes, python_data.sys_prefix)
        if build_dpkg:
            packages["build_dpkg"] = build_dpkg  # type: ignore[assignment]
            self.logger.debug("Build tool dpkg packages: %d", len(build_dpkg))

        # 8c. Collect pip-installed build tool dependencies from process tree
        self.logger.debug("Collecting pip-installed build tool dependencies")
        build_pip_collector = BuildPipCollectorService(logger=self._logger)
        build_pip = build_pip_collector.collect(tracer_data.processes, python_data.sys_prefix)
        if build_pip:
            packages["build_pip"] = build_pip  # type: ignore[assignment]
            self.logger.debug("Build tool pip packages: %d", len(build_pip))

        # 9. Run analyzers
        self.logger.debug("Running analyzers")
        analyzer_context = {
            "repo_root": repo_root,
            "written_files": filtered_files.written_files,
            "read_files": filtered_files.read_files,
            "env": python_data.env_reads or self._get_env_from_processes(tracer_data.processes),
            "processes": process_info,
            "tracer_data": {
                "opened_files": tracer_data.opened_files,
                "read_files": tracer_data.read_files,
                "written_files": tracer_data.written_files,
                "processes": tracer_data.processes,
                "start_time": tracer_data.start_time,
                "end_time": tracer_data.end_time,
            },
            "python_data": {
                "modules_files": python_data.modules_files,
                "env_reads": python_data.env_reads,
                "sys_prefix": python_data.sys_prefix,
                "sys_base_prefix": python_data.sys_base_prefix,
                "roar_inject_dir": python_data.roar_inject_dir,
                "shared_libs": python_data.shared_libs,
                "used_packages": python_data.used_packages,
                "installed_packages": python_data.installed_packages,
            },
        }
        analyzer_results = analyzers.run_analyzers(analyzer_context, config=config)
        self.logger.debug(
            "Analyzers complete: %d results", len(analyzer_results) if analyzer_results else 0
        )

        # 10. Assemble output
        self.logger.debug("Assembling final provenance output")
        ctx = ProvenanceContext(
            repo_root=repo_root,
            tracer_data=tracer_data,
            python_data=python_data,
            filtered_files=filtered_files,
            runtime_info=runtime_info,
            process_summary=process_summary,
            classification=classification,
            git_info=git_info,
            packages=packages,
            analyzer_results=analyzer_results,
        )

        result = self._assembler.assemble(ctx, config)
        self.logger.debug("Provenance collection complete")
        return result

    def _build_timing(self, start_time: float, end_time: float) -> dict[str, Any]:
        """Build timing info dict from timestamps."""
        if not start_time or not end_time:
            return {}

        return {
            "start": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat(),
            "duration_seconds": end_time - start_time,
        }

    def _build_process_info(self, processes: list) -> list:
        """Build process info list for summarization."""
        process_info = []
        for proc in processes:
            process_info.append(
                {
                    "pid": proc.get("pid"),
                    "parent_pid": proc.get("parent_pid"),
                    "command": proc.get("command", []),
                }
            )
        return process_info

    def _get_git_info(self, repo_root: str) -> dict[str, Any]:
        """Get git info via VCS provider."""
        vcs = get_container().get_vcs_provider("git")
        vcs_info = vcs.get_info(repo_root)

        return {
            "commit": vcs_info.commit,
            "branch": vcs_info.branch,
            "remote_url": vcs_info.remote_url,
            "clean": vcs_info.clean,
            "uncommitted_changes": vcs_info.uncommitted_changes if not vcs_info.clean else None,
            "commit_timestamp": vcs_info.commit_timestamp,
            "commit_message": vcs_info.commit_message,
        }

    def _get_env_from_processes(self, processes: list) -> dict[str, str]:
        """Get environment from root process if Python data unavailable."""
        if not processes:
            return {}

        root_proc = next((p for p in processes if p.get("parent_pid") is None), None)
        if root_proc:
            return root_proc.get("env", {})
        return {}
