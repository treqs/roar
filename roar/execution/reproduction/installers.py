"""
Installer strategies for reproduction environment setup.

These classes isolate package-manager-specific logic from orchestration.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.interfaces.logger import ILogger
    from ...core.interfaces.presenter import IPresenter


class AptInstaller:
    """Install Debian packages via apt-get."""

    def __init__(
        self,
        presenter: IPresenter | None = None,
        print_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._presenter = presenter
        self._print = print_fn or print

    def install(
        self,
        packages: dict[str, str],
        auto_confirm: bool,
        allow_any_version: bool = False,
        logger: ILogger | None = None,
        presenter: IPresenter | None = None,
        is_debian_based: Callable[[], bool] | None = None,
        is_root: Callable[[], bool] | None = None,
        is_interactive: Callable[[], bool] | None = None,
    ) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        active_presenter = presenter or self._presenter
        debian_check = is_debian_based or self._is_debian_based
        root_check = is_root or self._is_root
        interactive_check = is_interactive or self._is_interactive

        if logger:
            logger.debug("AptInstaller.install starting with %d packages", len(packages))

        if not debian_check():
            if logger:
                logger.debug("Skipping apt install: non-Debian system")
            self._print("Skipping dpkg packages: not a Debian-based system")
            return True, ["dpkg packages skipped: non-Debian system"]

        needs_sudo = not root_check()
        if logger:
            logger.debug("Apt installer root=%s needs_sudo=%s", not needs_sudo, needs_sudo)

        if needs_sudo and not interactive_check() and not auto_confirm:
            self._print("Skipping dpkg packages: sudo required but not in interactive mode")
            return True, ["dpkg packages skipped: non-interactive terminal"]

        if not auto_confirm:
            self._print(f"\nSystem packages required ({len(packages)}):")
            for name, version in list(packages.items())[:10]:
                self._print(f"  - {name}={version}" if version else f"  - {name}")
            if len(packages) > 10:
                self._print(f"  ... and {len(packages) - 10} more")
            if needs_sudo:
                self._print("\nThese packages require sudo to install.")

            if active_presenter:
                if not active_presenter.confirm("Install system packages?", default=False):
                    return True, ["dpkg packages skipped by user"]
            else:
                response = input("Install system packages? [y/N] ").strip().lower()
                if response not in ("y", "yes"):
                    return True, ["dpkg packages skipped by user"]

        versioned = [f"{name}={version}" if version else name for name, version in packages.items()]
        self._print(f"Installing {len(packages)} system packages (exact versions)...")

        cmd_prefix = ["sudo"] if needs_sudo else []
        try:
            result = subprocess.run(
                [*cmd_prefix, "apt-get", "install", "-y", *versioned],
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
            if result.stderr:
                self._print(result.stderr.strip())

            if result.returncode == 0:
                self._print("System packages installed successfully")
                return True, warnings

            failed_packages: list[str] = []
            succeeded_packages: list[str] = []
            for name, version in packages.items():
                spec = f"{name}={version}" if version else name
                dry_run = subprocess.run(
                    [*cmd_prefix, "apt-get", "install", "-y", "--dry-run", spec],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if dry_run.returncode != 0:
                    failed_packages.append(name)
                else:
                    succeeded_packages.append(spec)

            if succeeded_packages:
                result = subprocess.run(
                    [*cmd_prefix, "apt-get", "install", "-y", *succeeded_packages],
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300,
                )
                if result.stderr:
                    self._print(result.stderr.strip())

            if failed_packages:
                self._print(f"\nExact versions not found for {len(failed_packages)} packages:")
                for name in failed_packages:
                    self._print(f"  - {name}={packages[name]}")

                install_any = allow_any_version
                if not install_any and not auto_confirm:
                    if active_presenter:
                        install_any = active_presenter.confirm(
                            "Install available versions instead?", default=True
                        )
                    else:
                        response = (
                            input("Install available versions instead? [Y/n] ").strip().lower()
                        )
                        install_any = response not in ("n", "no")

                if install_any:
                    self._print(
                        f"Installing any available version of {len(failed_packages)} packages..."
                    )
                    fallback = subprocess.run(
                        [*cmd_prefix, "apt-get", "install", "-y", *failed_packages],
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=300,
                    )
                    if fallback.stderr:
                        self._print(fallback.stderr.strip())
                    if fallback.returncode != 0:
                        warnings.append(
                            f"Some packages failed to install: {fallback.stderr.strip()}"
                        )
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

    def _is_debian_based(self) -> bool:
        if platform.system() != "Linux":
            return False
        return shutil.which("apt-get") is not None

    def _is_root(self) -> bool:
        return os.geteuid() == 0

    def _is_interactive(self) -> bool:
        return sys.stdin.isatty()


class PythonPackageInstaller:
    """Install Python packages via pip or uv pip."""

    def __init__(
        self,
        use_uv: bool,
        presenter: IPresenter | None = None,
        print_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._use_uv = use_uv
        self._presenter = presenter
        self._print = print_fn or print

    @property
    def use_uv(self) -> bool:
        """Whether uv should be used instead of pip."""
        return self._use_uv

    @use_uv.setter
    def use_uv(self, enabled: bool) -> None:
        self._use_uv = enabled

    def install_build_tools(
        self,
        venv_dir: Path,
        packages: dict[str, str],
        repo_dir: Path,
        allow_any_version: bool = False,
        logger: ILogger | None = None,
        presenter: IPresenter | None = None,
    ) -> None:
        if not packages:
            return

        _ = presenter or self._presenter
        self._print(f"Installing {len(packages)} build tool pip packages...")
        specs = [f"{name}=={version}" if version else name for name, version in packages.items()]
        result = self._run_pip(venv_dir, repo_dir, ["install", *specs], show_output=True)
        if result.returncode == 0:
            self._print("Build tool pip packages installed successfully")
            return

        if logger:
            logger.warning("Build pip install failed: %s", (result.stderr or "").strip())

        if not allow_any_version:
            return

        self._print("Retrying build tool pip packages without version pins...")
        unversioned = list(packages.keys())
        self._run_pip(venv_dir, repo_dir, ["install", *unversioned], show_output=True)

    def install_packages(
        self,
        venv_dir: Path,
        packages: list[str],
        repo_dir: Path,
        auto_confirm: bool = False,
        allow_any_version: bool = False,
        logger: ILogger | None = None,
        presenter: IPresenter | None = None,
    ) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        active_presenter = presenter or self._presenter
        if not packages:
            self._print("No packages to install from provenance.")
            return True, warnings

        self._print(f"Installing {len(packages)} packages from provenance...")
        result = self._run_pip(venv_dir, repo_dir, ["install", *packages], show_output=True)
        if result.returncode == 0:
            self._print("All pip packages installed successfully")
            return True, warnings

        if logger:
            logger.debug("Batch pip install failed: %s", (result.stderr or "").strip())

        failed_packages: list[str] = []
        succeeded_packages: list[str] = []
        for package in packages:
            dry_run = self._run_pip(venv_dir, repo_dir, ["install", "--dry-run", package], True)
            if dry_run.returncode != 0:
                failed_packages.append(package)
            else:
                succeeded_packages.append(package)

        if succeeded_packages:
            self._run_pip(venv_dir, repo_dir, ["install", *succeeded_packages], show_output=True)

        if failed_packages:
            self._print(f"\nExact versions not found for {len(failed_packages)} pip packages:")
            for package in failed_packages:
                self._print(f"  - {package}")

            install_any = allow_any_version
            if not install_any and not auto_confirm:
                if active_presenter:
                    install_any = active_presenter.confirm(
                        "Install available versions instead?", default=True
                    )
                else:
                    response = input("Install available versions instead? [Y/n] ").strip().lower()
                    install_any = response not in ("n", "no")

            if install_any:
                unversioned = [package.split("==")[0] for package in failed_packages]
                self._print(f"Installing any available version of {len(unversioned)} packages...")
                fallback = self._run_pip(venv_dir, repo_dir, ["install", *unversioned], True)
                if fallback.returncode != 0:
                    warnings.append(
                        f"Some pip packages failed to install: {(fallback.stderr or '').strip()}"
                    )
                else:
                    for package in failed_packages:
                        warnings.append(
                            f"Installed {package.split('==')[0]} (any version) instead of {package}"
                        )
            else:
                for package in failed_packages:
                    warnings.append(f"Skipped {package} (exact version not found)")

        self._print("Pip package installation complete")
        return True, warnings

    def _run_pip(
        self,
        venv_dir: Path,
        repo_dir: Path,
        args: list[str],
        show_output: bool,
    ) -> subprocess.CompletedProcess[str]:
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

        pip_path = self._get_pip(venv_dir)
        return subprocess.run(  # type: ignore[call-overload]
            [str(pip_path), *args],
            cwd=repo_dir,
            **capture_kwargs,
        )

    @staticmethod
    def _get_pip(venv_dir: Path) -> Path:
        if sys.platform == "win32":
            return venv_dir / "Scripts" / "pip"
        return venv_dir / "bin" / "pip"
