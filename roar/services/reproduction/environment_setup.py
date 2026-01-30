"""
Environment setup service for reproduction.

Extracted from reproduce.py to follow Single Responsibility Principle.
This service handles git cloning, virtual environment creation, and
package installation for reproduction.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...utils.git_url import is_ssh_url, ssh_to_https

if TYPE_CHECKING:
    from ...core.interfaces.logger import ILogger
    from ...core.interfaces.presenter import IPresenter
    from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo


class EnvironmentSetupService:
    """
    Service for setting up reproduction environments.

    Handles:
    - Git repository cloning and checkout
    - Virtual environment creation
    - System package installation (via apt-get for dpkg)
    - Python package installation (via pip or uv)

    Usage:
        service = EnvironmentSetupService(presenter)
        env = service.setup(pipeline, target_dir, auto_confirm=True)
    """

    def __init__(
        self,
        presenter: "IPresenter | None" = None,
        roar_executable: str | None = None,
    ):
        """
        Initialize environment setup service.

        Args:
            presenter: Presenter for user feedback
            roar_executable: Path to roar executable for initialization
        """
        self._presenter = presenter
        self._use_uv = self._check_uv_available()
        self._roar_executable = roar_executable or self._detect_roar_executable()
        self._logger: ILogger | None = None

    @property
    def logger(self) -> "ILogger":
        """Lazy-load logger from container."""
        if self._logger is None:
            from ...core.container import get_container
            from ...services.logging import NullLogger

            container = get_container()
            from ...core.interfaces.logger import ILogger

            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def setup(
        self,
        pipeline: "PipelineInfo",
        target_dir: Path,
        auto_confirm: bool = False,
        dpkg_any_version: bool = False,
        pip_any_version: bool = False,
        package_sync: bool = False,
    ) -> "EnvironmentInfo":
        """
        Set up reproduction environment.

        Args:
            pipeline: Pipeline information with git repo and packages
            target_dir: Directory to set up the environment in
            auto_confirm: Skip confirmation prompts
            dpkg_any_version: Install any available version of dpkg packages
                when exact version not found
            pip_any_version: Install any available version of pip packages
                when exact version not found

        Returns:
            EnvironmentInfo with setup details

        Raises:
            RuntimeError: If setup fails
        """
        from ...core.interfaces.reproduction import EnvironmentInfo

        self.logger.debug("EnvironmentSetupService.setup: starting environment setup")
        self.logger.debug("Target directory: %s", target_dir)
        self.logger.debug("Git repo: %s, commit: %s", pipeline.git_repo, pipeline.git_commit)

        # Validate environment first
        env_warnings = self._validate_environment(pipeline)
        if env_warnings:
            self._print("\nEnvironment warnings:")
            for warning in env_warnings:
                self._print(f"  - {warning}")
            self._print("")

        # Clone repository
        self.logger.debug("Cloning repository...")
        repo_dir = self._clone_repository(
            pipeline.git_repo,
            pipeline.git_commit,
            target_dir,
        )
        self.logger.debug("Repository cloned to: %s", repo_dir)

        # Create virtual environment
        self.logger.debug("Creating virtual environment...")
        venv_dir = self._create_venv(repo_dir)
        self.logger.debug("Virtual environment created at: %s", venv_dir)

        # Initialize roar in the cloned repository
        # Note: We no longer install roar into the venv - we use the external
        # roar executable to avoid being deleted by 'uv sync' build steps
        self.logger.debug("Initializing roar in cloned repository...")
        self._initialize_roar(repo_dir, venv_dir)
        self.logger.debug("Roar initialized")

        # Install build tool dpkg packages first (needed for source compilations)
        if package_sync:
            build_dpkg_packages = self._get_build_dpkg_packages(pipeline)
            self.logger.debug("Found %d build_dpkg packages", len(build_dpkg_packages))
            if build_dpkg_packages:
                self.logger.debug("build_dpkg packages: %s", build_dpkg_packages)
                success, _build_warnings = self._install_dpkg_packages(
                    build_dpkg_packages, auto_confirm, dpkg_any_version
                )
                self.logger.debug("build_dpkg installation complete, success=%s", success)

            # Install dpkg packages BEFORE pip packages (system deps first)
            dpkg_packages = self._get_dpkg_packages(pipeline)
            self.logger.debug(
                "Found %d dpkg packages on job, intending to install: %d",
                len(dpkg_packages),
                len(dpkg_packages),
            )
            if dpkg_packages:
                self.logger.debug("dpkg packages: %s", dpkg_packages)
                success, _dpkg_warnings = self._install_dpkg_packages(
                    dpkg_packages, auto_confirm, dpkg_any_version
                )
                self.logger.debug("dpkg installation complete, success=%s", success)
        else:
            self.logger.debug("Skipping system package installation (--package-sync not set)")

        # Install pip-installed build tools (before regular pip packages)
        build_pip_packages = self._get_build_pip_packages(pipeline)
        self.logger.debug("Found %d build_pip packages", len(build_pip_packages))
        if build_pip_packages:
            self.logger.debug("build_pip packages: %s", build_pip_packages)
            self._install_build_pip_packages(
                venv_dir, build_pip_packages, repo_dir, pip_any_version
            )

        # Install pip packages
        packages = self._get_packages(pipeline)
        self.logger.debug(
            "Found %d pip packages on job, intending to install: %d",
            len(packages),
            len(packages),
        )
        if packages:
            self.logger.debug("pip packages: %s", packages[:10])
            success, pip_warnings = self._install_packages(
                venv_dir, packages, repo_dir, auto_confirm, pip_any_version
            )
            if pip_warnings:
                for w in pip_warnings:
                    self.logger.warning(w)
            self.logger.debug("pip installation complete")

        self.logger.debug("Environment setup complete")
        return EnvironmentInfo(
            repo_dir=repo_dir,
            venv_dir=venv_dir,
            python_version=self._get_python_version(),
            packages=packages,
        )

    def _is_debian_based(self) -> bool:
        """Check if the current system is Debian-based Linux."""
        if platform.system() != "Linux":
            return False
        return shutil.which("apt-get") is not None

    def _is_root(self) -> bool:
        """Check if running with root privileges."""
        return os.geteuid() == 0

    def _is_interactive(self) -> bool:
        """Check if stdin is a TTY."""
        return sys.stdin.isatty()

    def _get_build_dpkg_packages(self, pipeline: "PipelineInfo") -> dict[str, str]:
        """Extract build_dpkg package dict {name: version} from pipeline metadata."""
        import json

        packages: dict[str, str] = {}

        def _extract_build_dpkg_from_steps(steps: list) -> dict[str, str]:
            result: dict[str, str] = {}
            for step in steps:
                metadata = step.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        continue

                pkgs_by_manager = metadata.get("packages", {})
                build_dpkg_pkgs = pkgs_by_manager.get("build_dpkg", {})

                if isinstance(build_dpkg_pkgs, dict):
                    for name, version in build_dpkg_pkgs.items():
                        if name and name not in result:
                            result[name] = version or ""
            return result

        build_pkgs = _extract_build_dpkg_from_steps(pipeline.build_steps)
        run_pkgs = _extract_build_dpkg_from_steps(pipeline.run_steps)
        self.logger.debug("build_dpkg packages from build steps: %d", len(build_pkgs))
        self.logger.debug("build_dpkg packages from run steps: %d", len(run_pkgs))

        packages = {**build_pkgs, **run_pkgs}
        return dict(sorted(packages.items()))

    def _get_build_pip_packages(self, pipeline: "PipelineInfo") -> dict[str, str]:
        """Extract build_pip package dict {name: version} from pipeline metadata."""
        import json

        packages: dict[str, str] = {}

        def _extract_build_pip_from_steps(steps: list) -> dict[str, str]:
            result: dict[str, str] = {}
            for step in steps:
                metadata = step.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        continue

                pkgs_by_manager = metadata.get("packages", {})
                build_pip_pkgs = pkgs_by_manager.get("build_pip", {})

                if isinstance(build_pip_pkgs, dict):
                    for name, version in build_pip_pkgs.items():
                        if name and name not in result:
                            result[name] = version or ""
            return result

        build_pkgs = _extract_build_pip_from_steps(pipeline.build_steps)
        run_pkgs = _extract_build_pip_from_steps(pipeline.run_steps)
        self.logger.debug("build_pip packages from build steps: %d", len(build_pkgs))
        self.logger.debug("build_pip packages from run steps: %d", len(run_pkgs))

        packages = {**build_pkgs, **run_pkgs}
        return dict(sorted(packages.items()))

    def _install_build_pip_packages(
        self,
        venv_dir: Path,
        packages: dict[str, str],
        repo_dir: Path,
        pip_any_version: bool = False,
    ) -> None:
        """Install pip-installed build tools into the venv before regular packages."""
        if not packages:
            return

        self._print(f"Installing {len(packages)} build tool pip packages...")

        # Build versioned specifiers: "pkg==version"
        specs = [f"{name}=={version}" if version else name for name, version in packages.items()]

        if self._use_uv:
            result = subprocess.run(
                ["uv", "pip", "install", *specs],
                cwd=repo_dir,
                env={"VIRTUAL_ENV": str(venv_dir), "PATH": os.environ.get("PATH", "")},
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            pip = self._get_pip(venv_dir)
            result = subprocess.run(
                [str(pip), "install", *specs],
                cwd=repo_dir,
                stderr=subprocess.PIPE,
                text=True,
            )

        if result.returncode != 0:
            self.logger.warning("Build pip install failed: %s", result.stderr.strip())
            if pip_any_version:
                # Retry without version pins
                unversioned = list(packages.keys())
                self._print("Retrying build tool pip packages without version pins...")
                if self._use_uv:
                    subprocess.run(
                        ["uv", "pip", "install", *unversioned],
                        cwd=repo_dir,
                        env={"VIRTUAL_ENV": str(venv_dir), "PATH": os.environ.get("PATH", "")},
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                else:
                    pip = self._get_pip(venv_dir)
                    subprocess.run(
                        [str(pip), "install", *unversioned],
                        cwd=repo_dir,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
        else:
            self._print("Build tool pip packages installed successfully")

    def _get_dpkg_packages(self, pipeline: "PipelineInfo") -> dict[str, str]:
        """Extract dpkg package dict {name: version} from pipeline metadata."""
        import json

        packages: dict[str, str] = {}

        def _extract_dpkg_from_steps(steps: list) -> dict[str, str]:
            result: dict[str, str] = {}
            for step in steps:
                metadata = step.get("metadata") or {}
                self.logger.debug(
                    "Step metadata type=%s, value=%s",
                    type(metadata).__name__,
                    repr(metadata)[:200] if metadata else "None",
                )
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        continue

                pkgs_by_manager = metadata.get("packages", {})
                dpkg_pkgs = pkgs_by_manager.get("dpkg", {})

                if isinstance(dpkg_pkgs, dict):
                    for name, version in dpkg_pkgs.items():
                        if name and name not in result:
                            result[name] = version or ""
            return result

        build_pkgs = _extract_dpkg_from_steps(pipeline.build_steps)
        run_pkgs = _extract_dpkg_from_steps(pipeline.run_steps)
        self.logger.debug("dpkg packages from build steps: %d", len(build_pkgs))
        self.logger.debug("dpkg packages from run steps: %d", len(run_pkgs))

        packages = {**build_pkgs, **run_pkgs}
        self.logger.debug("Total unique dpkg packages found: %d", len(packages))
        return dict(sorted(packages.items()))

    def _install_dpkg_packages(
        self,
        packages: dict[str, str],
        auto_confirm: bool,
        dpkg_any_version: bool = False,
    ) -> tuple[bool, list[str]]:
        """Install dpkg packages via apt-get. Returns (success, warnings).

        Strategy:
        1. Try installing all packages with exact versions
        2. For any that fail, warn and prompt to install without version pin
        3. If --dpkg-any-version, skip prompt and install any available version
        """
        warnings: list[str] = []
        self.logger.debug("_install_dpkg_packages: starting with %d packages", len(packages))

        if not self._is_debian_based():
            self.logger.debug("Skipping: not Debian-based system")
            self._print("Skipping dpkg packages: not a Debian-based system")
            return True, ["dpkg packages skipped: non-Debian system"]

        needs_sudo = not self._is_root()
        self.logger.debug("Running as root: %s, needs sudo: %s", not needs_sudo, needs_sudo)

        if needs_sudo and not self._is_interactive() and not auto_confirm:
            self.logger.debug("Skipping: non-interactive terminal, sudo required")
            self._print("Skipping dpkg packages: sudo required but not in interactive mode")
            return True, ["dpkg packages skipped: non-interactive terminal"]

        # Confirmation prompt
        if not auto_confirm:
            self._print(f"\nSystem packages required ({len(packages)}):")
            for name, version in list(packages.items())[:10]:
                self._print(f"  - {name}={version}" if version else f"  - {name}")
            if len(packages) > 10:
                self._print(f"  ... and {len(packages) - 10} more")

            if needs_sudo:
                self._print("\nThese packages require sudo to install.")

            if self._presenter:
                if not self._presenter.confirm("Install system packages?", default=False):
                    return True, ["dpkg packages skipped by user"]
            else:
                response = input("Install system packages? [y/N] ").strip().lower()
                if response not in ("y", "yes"):
                    return True, ["dpkg packages skipped by user"]

        # Build versioned package specifiers: "pkg=version" for apt-get
        versioned = [f"{name}={version}" if version else name for name, version in packages.items()]

        self.logger.debug("Attempting versioned install: %s", versioned)
        self._print(f"Installing {len(packages)} system packages (exact versions)...")

        cmd_prefix = ["sudo"] if needs_sudo else []
        try:
            cmd = [*cmd_prefix, "apt-get", "install", "-y", *versioned]
            self.logger.debug("Running command: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
            self.logger.debug("apt-get returned: %d", result.returncode)
            if result.stderr:
                self._print(result.stderr.strip())

            if result.returncode == 0:
                self.logger.debug("All dpkg packages installed with exact versions")
                self._print("System packages installed successfully")
                return True, warnings

            # Exact version failed — identify which packages failed
            self.logger.debug("Versioned install failed: %s", result.stderr.strip())

            # Try each package individually to find failures
            failed_packages: list[str] = []
            succeeded_packages: list[str] = []

            for name, version in packages.items():
                spec = f"{name}={version}" if version else name
                r = subprocess.run(
                    [*cmd_prefix, "apt-get", "install", "-y", "--dry-run", spec],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if r.returncode != 0:
                    failed_packages.append(name)
                    self.logger.debug("Package %s version %s not available", name, version)
                else:
                    succeeded_packages.append(spec)

            # Install the ones that work with exact versions
            if succeeded_packages:
                self.logger.debug(
                    "Installing %d packages with exact versions", len(succeeded_packages)
                )
                result = subprocess.run(
                    [*cmd_prefix, "apt-get", "install", "-y", *succeeded_packages],
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300,
                )
                if result.stderr:
                    self._print(result.stderr.strip())

            # Handle failed packages — offer to install any version
            if failed_packages:
                self._print(f"\nExact versions not found for {len(failed_packages)} packages:")
                for name in failed_packages:
                    self._print(f"  - {name}={packages[name]}")

                install_any = dpkg_any_version
                if not install_any and not auto_confirm:
                    if self._presenter:
                        install_any = self._presenter.confirm(
                            "Install available versions instead?", default=True
                        )
                    else:
                        resp = input("Install available versions instead? [Y/n] ").strip().lower()
                        install_any = resp not in ("n", "no")

                if install_any:
                    self.logger.debug("Installing any version of: %s", failed_packages)
                    self._print(
                        f"Installing any available version of {len(failed_packages)} packages..."
                    )
                    r = subprocess.run(
                        [*cmd_prefix, "apt-get", "install", "-y", *failed_packages],
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=300,
                    )
                    if r.stderr:
                        self._print(r.stderr.strip())
                    if r.returncode != 0:
                        warnings.append(f"Some packages failed to install: {r.stderr.strip()}")
                        self.logger.warning("Fallback install failed: %s", r.stderr.strip())
                    else:
                        for name in failed_packages:
                            warnings.append(
                                f"Installed {name} (any version) instead of {name}={packages[name]}"
                            )
                else:
                    for name in failed_packages:
                        warnings.append(
                            f"Skipped {name}={packages[name]} (exact version not found)"
                        )

            self._print("System package installation complete")
            return True, warnings

        except subprocess.TimeoutExpired:
            warnings.append("dpkg installation timed out after 5 minutes")
            return True, warnings
        except Exception as e:
            warnings.append(f"dpkg installation error: {e!s}")
            return True, warnings

    def _validate_environment(self, pipeline: "PipelineInfo") -> list[str]:
        """
        Compare current system with the original execution environment.
        Returns list of warning messages for mismatches.
        """
        import json

        warnings: list[str] = []

        # Collect runtime info from all steps
        original_runtime: dict = {}
        for step in pipeline.build_steps + pipeline.run_steps:
            metadata = step.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    continue

            runtime = metadata.get("runtime", {})
            if runtime and not original_runtime:
                original_runtime = runtime
                break

        if not original_runtime:
            self.logger.debug("No runtime metadata found in pipeline steps")
            return warnings

        # Check OS
        orig_os = original_runtime.get("os", {})
        current_system = platform.system()
        if orig_os.get("system") and orig_os["system"] != current_system:
            msg = f"OS mismatch: original={orig_os['system']}, current={current_system}"
            warnings.append(msg)
            self.logger.warning(msg)

        # Check architecture
        current_machine = platform.machine()
        orig_machine = orig_os.get("machine")
        if orig_machine and orig_machine != current_machine:
            msg = f"Architecture mismatch: original={orig_machine}, current={current_machine}"
            warnings.append(msg)
            self.logger.warning(msg)

        # Check CPU architecture (more detailed)
        orig_cpu = original_runtime.get("cpu", {})
        if orig_cpu.get("architecture") and orig_cpu["architecture"] != current_machine:
            msg = (
                f"CPU architecture mismatch: original={orig_cpu['architecture']}, "
                f"current={current_machine}"
            )
            if msg not in warnings:
                warnings.append(msg)
                self.logger.warning(msg)

        # Check CUDA
        orig_cuda = original_runtime.get("cuda", {})
        if orig_cuda:
            current_cuda = self._get_current_cuda_version()
            if current_cuda is None:
                msg = (
                    f"CUDA required (version {orig_cuda.get('cuda_version', 'unknown')}) "
                    f"but not available"
                )
                warnings.append(msg)
                self.logger.warning(msg)
            elif orig_cuda.get("cuda_version") and orig_cuda["cuda_version"] != current_cuda:
                msg = (
                    f"CUDA version mismatch: original={orig_cuda['cuda_version']}, "
                    f"current={current_cuda}"
                )
                warnings.append(msg)
                self.logger.warning(msg)

        # Check GPU availability
        orig_gpu = original_runtime.get("gpu", [])
        if orig_gpu:
            current_gpu = self._check_gpu_available()
            if not current_gpu:
                gpu_names = [g.get("name", "unknown") for g in orig_gpu]
                msg = f"GPU required ({', '.join(gpu_names)}) but not detected"
                warnings.append(msg)
                self.logger.warning(msg)

        return warnings

    def _get_current_cuda_version(self) -> str | None:
        """Get current CUDA version from nvcc."""
        try:
            result = subprocess.run(
                ["nvcc", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "release" in line.lower():
                        parts = line.split("release")
                        if len(parts) > 1:
                            version = parts[1].strip().split(",")[0].strip()
                            return version
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _check_gpu_available(self) -> bool:
        """Check if nvidia-smi can detect a GPU."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "-L"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and "GPU" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _clone_repository(
        self,
        git_repo: str | None,
        git_commit: str | None,
        target_dir: Path,
    ) -> Path:
        """
        Clone git repository and checkout commit.

        Returns:
            Path to cloned repository
        """
        if not git_repo:
            raise RuntimeError("No git repository URL available for reproduction")

        # Create target directory
        target_dir.mkdir(parents=True, exist_ok=True)

        # Extract repo name from URL
        repo_name = git_repo.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        repo_dir = target_dir / repo_name

        # Clone repository
        if repo_dir.exists():
            # Directory exists, try to update it
            self._print(f"Repository directory exists: {repo_dir}")
            self._print("Fetching latest changes...")
            self._run_git(["fetch", "--all"], cwd=repo_dir)
        else:
            self._print(f"Cloning {git_repo}...")
            try:
                self._run_git(["clone", git_repo, str(repo_dir)])
            except RuntimeError:
                if is_ssh_url(git_repo):
                    https_url = ssh_to_https(git_repo)
                    if https_url:
                        self._print("SSH clone failed, trying HTTPS fallback...")
                        self._print(f"Cloning {https_url}...")
                        self._run_git(["clone", https_url, str(repo_dir)])
                    else:
                        raise
                else:
                    raise

        # Checkout specific commit
        if git_commit:
            self._print(f"Checking out commit {git_commit[:12]}...")
            self._run_git(["checkout", git_commit], cwd=repo_dir)

        return repo_dir

    def _create_venv(self, repo_dir: Path) -> Path:
        """
        Create virtual environment in repository.

        Returns:
            Path to venv directory
        """
        venv_dir = repo_dir / ".venv"

        if venv_dir.exists():
            self._print(f"Virtual environment exists: {venv_dir}")
            return venv_dir

        self._print("Creating virtual environment...")

        if self._use_uv:
            subprocess.run(
                ["uv", "venv", str(venv_dir)],
                check=True,
                cwd=repo_dir,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
                cwd=repo_dir,
            )

        gitignore = venv_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

        return venv_dir

    def _install_roar(self, venv_dir: Path, repo_dir: Path) -> None:
        """Install roar-cli into the virtual environment."""
        self._print("Installing roar for provenance tracking...")
        if self._use_uv:
            subprocess.run(
                ["uv", "pip", "install", "roar-cli"],
                check=True,
                cwd=repo_dir,
                env={"VIRTUAL_ENV": str(venv_dir), "PATH": os.environ.get("PATH", "")},
            )
        else:
            pip = self._get_pip(venv_dir)
            subprocess.run([str(pip), "install", "roar-cli"], check=True, cwd=repo_dir)

    def _initialize_roar(self, repo_dir: Path, venv_dir: Path) -> None:
        """Initialize roar in the cloned repository.

        Uses the external roar executable instead of the venv's python.
        """
        roar_dir = repo_dir / ".roar"
        if roar_dir.exists():
            self._print("Roar already initialized")
            return

        self._print("Initializing roar for provenance tracking...")
        # Use the external roar executable
        # Handle both single executable and "python -m roar" formats
        cmd = [*self._roar_executable.split(), "init", "-y"]
        subprocess.run(
            cmd,
            cwd=repo_dir,
            check=True,
        )

    def _get_pip(self, venv_dir: Path) -> Path:
        """Get path to pip executable in venv."""
        if sys.platform == "win32":
            return venv_dir / "Scripts" / "pip"
        return venv_dir / "bin" / "pip"

    def _get_venv_python(self, venv_dir: Path) -> Path:
        """Get path to Python executable in venv."""
        if sys.platform == "win32":
            return venv_dir / "Scripts" / "python.exe"
        return venv_dir / "bin" / "python"

    def _install_packages(
        self,
        venv_dir: Path,
        packages: list[str],
        repo_dir: Path,
        auto_confirm: bool = False,
        pip_any_version: bool = False,
    ) -> tuple[bool, list[str]]:
        """Install packages into virtual environment. Returns (success, warnings).

        Strategy:
        1. Try installing all packages with exact versions
        2. For any that fail, identify which ones and prompt to install without version pin
        3. If --pip-any-version, skip prompt and install any available version
        """
        warnings: list[str] = []

        if not packages:
            self._print("No packages to install from provenance.")
            return True, warnings

        self._print(f"Installing {len(packages)} packages from provenance...")

        pip = self._get_pip(venv_dir)

        def _run_pip(args: list[str], show_output: bool = True) -> subprocess.CompletedProcess[str]:
            capture_kwargs = (
                {"stderr": subprocess.PIPE, "text": True}
                if show_output
                else {"capture_output": True, "text": True}
            )
            if self._use_uv:
                result = subprocess.run(  # type: ignore[call-overload]
                    ["uv", "pip", *args],
                    cwd=repo_dir,
                    env={"VIRTUAL_ENV": str(venv_dir), "PATH": os.environ.get("PATH", "")},
                    **capture_kwargs,
                )
                if show_output and result.stderr:
                    self._print(result.stderr.strip())
                return result
            else:
                return subprocess.run(  # type: ignore[call-overload]
                    [str(pip), *args],
                    cwd=repo_dir,
                    **capture_kwargs,
                )

        # Step 1: Try installing all packages at once
        result = _run_pip(["install", *packages])

        if result.returncode == 0:
            self._print("All pip packages installed successfully")
            return True, warnings

        # Step 2: Batch install failed — identify which packages failed
        self.logger.debug("Batch pip install failed: %s", result.stderr.strip())

        failed_packages: list[str] = []
        succeeded_packages: list[str] = []

        for pkg in packages:
            r = _run_pip(["install", "--dry-run", pkg], show_output=True)
            if r.returncode != 0:
                failed_packages.append(pkg)
                self.logger.debug("Package %s not available", pkg)
            else:
                succeeded_packages.append(pkg)

        # Step 3: Install the ones that work with exact versions
        if succeeded_packages:
            self.logger.debug("Installing %d packages with exact versions", len(succeeded_packages))
            _run_pip(["install", *succeeded_packages])

        # Step 4: Handle failed packages — offer to install any version
        if failed_packages:
            self._print(f"\nExact versions not found for {len(failed_packages)} pip packages:")
            for pkg in failed_packages:
                self._print(f"  - {pkg}")

            install_any = pip_any_version
            if not install_any and not auto_confirm:
                if self._presenter:
                    install_any = self._presenter.confirm(
                        "Install available versions instead?", default=True
                    )
                else:
                    resp = input("Install available versions instead? [Y/n] ").strip().lower()
                    install_any = resp not in ("n", "no")

            if install_any:
                # Strip version pins (e.g. "numpy==1.24.1" -> "numpy")
                unversioned = [pkg.split("==")[0] for pkg in failed_packages]
                self.logger.debug("Installing any version of: %s", unversioned)
                self._print(f"Installing any available version of {len(unversioned)} packages...")
                r = _run_pip(["install", *unversioned])
                if r.returncode != 0:
                    warnings.append(f"Some pip packages failed to install: {r.stderr.strip()}")
                    self.logger.warning("Fallback pip install failed: %s", r.stderr.strip())
                else:
                    for pkg in failed_packages:
                        warnings.append(
                            f"Installed {pkg.split('==')[0]} (any version) instead of {pkg}"
                        )
            else:
                for pkg in failed_packages:
                    warnings.append(f"Skipped {pkg} (exact version not found)")

        self._print("Pip package installation complete")
        return True, warnings

    def _get_packages(self, pipeline: "PipelineInfo") -> list[str]:
        """Extract pip package list from pipeline metadata."""
        import json

        def _extract_pip_from_steps(steps: list) -> set[str]:
            result: set[str] = set()
            for step in steps:
                metadata = step.get("metadata") or {}
                self.logger.debug(
                    "Step metadata type=%s, value=%s",
                    type(metadata).__name__,
                    repr(metadata)[:200] if metadata else "None",
                )
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        continue

                # Format: {"packages": {"pip": {"numpy": "1.24.1"}, "dpkg": {...}}}
                pkgs_by_manager = metadata.get("packages", {})
                pip_packages = pkgs_by_manager.get("pip", {})

                if isinstance(pip_packages, dict):
                    for name, version in pip_packages.items():
                        if name:
                            result.add(f"{name}=={version}" if version else name)
            return result

        build_pkgs = _extract_pip_from_steps(pipeline.build_steps)
        run_pkgs = _extract_pip_from_steps(pipeline.run_steps)
        self.logger.debug("pip packages from build steps: %d", len(build_pkgs))
        self.logger.debug("pip packages from run steps: %d", len(run_pkgs))

        packages = build_pkgs | run_pkgs
        self.logger.debug("Total unique pip packages found: %d", len(packages))
        return sorted(packages)

    def _run_git(self, args: list[str], cwd: Path | None = None) -> None:
        """Run a git command."""
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Git command failed: {result.stderr}")

    def _check_uv_available(self) -> bool:
        """Check if uv is available."""
        return shutil.which("uv") is not None

    def _detect_roar_executable(self) -> str:
        """Get path to the currently running roar executable.

        Returns:
            Path to roar executable, or fallback to python -m roar
        """
        # Option 1: If roar is installed as a script on PATH
        roar_path = shutil.which("roar")
        if roar_path:
            return roar_path
        # Option 2: Use current Python to run roar module
        return f"{sys.executable} -m roar"

    def _get_python_version(self) -> str:
        """Get current Python version."""
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    def _print(self, message: str) -> None:
        """Print message via presenter or fallback to print."""
        if self._presenter:
            self._presenter.print(message)
        else:
            print(message)
