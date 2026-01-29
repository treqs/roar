"""
Unit tests for EnvironmentSetupService.

Tests that roar is NOT installed into the reproduce venv.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from roar.services.reproduction.environment_setup import EnvironmentSetupService


@pytest.fixture
def service():
    """Create EnvironmentSetupService with mocked logger."""
    svc = EnvironmentSetupService()
    svc._logger = MagicMock()
    return svc


@pytest.fixture
def mock_pipeline():
    """Create a mock PipelineInfo."""
    pipeline = MagicMock()
    pipeline.git_repo = "https://github.com/test/repo.git"
    pipeline.git_commit = "abc123def456789"
    pipeline.build_steps = []
    pipeline.run_steps = []
    return pipeline


class TestEnvironmentSetupSkipsRoarInstallation:
    """Test that EnvironmentSetupService does NOT install roar."""

    @pytest.fixture
    def mock_pipeline(self):
        """Create a mock PipelineInfo."""
        pipeline = MagicMock()
        pipeline.git_repo = "https://github.com/test/repo.git"
        pipeline.git_commit = "abc123def456789"
        pipeline.build_steps = []
        pipeline.run_steps = []
        return pipeline

    def test_setup_skips_roar_installation(self, mock_pipeline, tmp_path):
        """setup() should NOT call _install_roar()."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        with (
            patch.object(service, "_clone_repository") as mock_clone,
            patch.object(service, "_create_venv") as mock_venv,
            patch.object(service, "_install_roar") as mock_install_roar,
            patch.object(service, "_initialize_roar") as mock_init_roar,
            patch.object(service, "_install_packages") as mock_install_packages,
            patch.object(service, "_get_packages") as mock_get_packages,
            patch.object(service, "_validate_environment") as mock_validate,
        ):
            mock_clone.return_value = tmp_path / "repo"
            mock_venv.return_value = tmp_path / "repo" / ".venv"
            mock_get_packages.return_value = []
            mock_validate.return_value = []

            service.setup(mock_pipeline, tmp_path, auto_confirm=True)

            # _install_roar should NOT be called
            mock_install_roar.assert_not_called()

    def test_initialize_roar_still_runs(self, mock_pipeline, tmp_path):
        """setup() should still call _initialize_roar() (roar init)."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()

        with (
            patch.object(service, "_clone_repository") as mock_clone,
            patch.object(service, "_create_venv") as mock_venv,
            patch.object(service, "_install_roar") as mock_install_roar,
            patch.object(service, "_initialize_roar") as mock_init_roar,
            patch.object(service, "_install_packages") as mock_install_packages,
            patch.object(service, "_get_packages") as mock_get_packages,
            patch.object(service, "_validate_environment") as mock_validate,
        ):
            mock_clone.return_value = tmp_path / "repo"
            mock_venv.return_value = tmp_path / "repo" / ".venv"
            mock_get_packages.return_value = []
            mock_validate.return_value = []

            service.setup(mock_pipeline, tmp_path, auto_confirm=True)

            # _initialize_roar SHOULD still be called
            mock_init_roar.assert_called_once()


class TestCreateVenvGitignore:
    """Test that _create_venv creates .gitignore in the venv directory."""

    def test_creates_gitignore_when_missing(self, tmp_path):
        """_create_venv should create .gitignore with '*' in the venv dir."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()
        service._use_uv = False

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            # subprocess.run won't actually create the venv dir, so create it
            # to simulate what `python -m venv` would do
            def create_venv_dir(*args, **kwargs):
                (repo_dir / ".venv").mkdir(exist_ok=True)
                return MagicMock(returncode=0)

            mock_run.side_effect = create_venv_dir

            venv_dir = service._create_venv(repo_dir)

        gitignore = venv_dir / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == "*\n"

    def test_does_not_overwrite_existing_gitignore(self, tmp_path):
        """_create_venv should not overwrite an existing .gitignore."""
        service = EnvironmentSetupService()
        service._logger = MagicMock()
        service._use_uv = True

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            def create_venv_with_gitignore(*args, **kwargs):
                venv = repo_dir / ".venv"
                venv.mkdir(exist_ok=True)
                (venv / ".gitignore").write_text("# created by uv\n*\n")
                return MagicMock(returncode=0)

            mock_run.side_effect = create_venv_with_gitignore

            venv_dir = service._create_venv(repo_dir)

        gitignore = venv_dir / ".gitignore"
        assert gitignore.read_text() == "# created by uv\n*\n"


class TestGetPackages:
    """Test _get_packages extracts pip packages from metadata."""

    @pytest.fixture
    def service(self):
        """Create EnvironmentSetupService."""
        svc = EnvironmentSetupService()
        svc._logger = MagicMock()
        return svc

    @pytest.fixture
    def mock_pipeline(self):
        """Create a mock PipelineInfo."""
        pipeline = MagicMock()
        pipeline.build_steps = []
        pipeline.run_steps = []
        return pipeline

    def test_extracts_pip_packages_from_metadata(self, service, mock_pipeline):
        """Should extract pip packages from metadata.packages.pip."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "packages": {
                        "pip": {"numpy": "1.24.1", "pandas": "2.0.0"}
                    }
                }
            }
        ]

        packages = service._get_packages(mock_pipeline)

        assert "numpy==1.24.1" in packages
        assert "pandas==2.0.0" in packages

    def test_ignores_dpkg_packages(self, service, mock_pipeline):
        """Should only extract pip packages, not dpkg."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "packages": {
                        "pip": {"requests": "2.31.0"},
                        "dpkg": {"libc6": "2.35-0ubuntu3"}
                    }
                }
            }
        ]

        packages = service._get_packages(mock_pipeline)

        assert "requests==2.31.0" in packages
        assert len(packages) == 1  # Only pip package

    def test_handles_missing_metadata(self, service, mock_pipeline):
        """Should handle steps with no metadata."""
        mock_pipeline.run_steps = [
            {},  # No metadata
            {"metadata": None},  # Explicit None
            {"metadata": {"packages": {}}},  # Empty packages
        ]

        packages = service._get_packages(mock_pipeline)

        assert packages == []

    def test_handles_malformed_json(self, service, mock_pipeline):
        """Should skip malformed JSON gracefully."""
        mock_pipeline.run_steps = [
            {"metadata": "not valid json {"},
            {"metadata": {"packages": {"pip": {"valid": "1.0.0"}}}},
        ]

        packages = service._get_packages(mock_pipeline)

        assert packages == ["valid==1.0.0"]

    def test_deduplicates_across_steps(self, service, mock_pipeline):
        """Should deduplicate packages from multiple steps."""
        mock_pipeline.build_steps = [
            {"metadata": {"packages": {"pip": {"numpy": "1.24.1"}}}}
        ]
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"pip": {"numpy": "1.24.1", "pandas": "2.0.0"}}}}
        ]

        packages = service._get_packages(mock_pipeline)

        assert packages == ["numpy==1.24.1", "pandas==2.0.0"]

    def test_handles_package_without_version(self, service, mock_pipeline):
        """Should handle packages with no version specified."""
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"pip": {"somepackage": None}}}}
        ]

        packages = service._get_packages(mock_pipeline)

        assert packages == ["somepackage"]

    def test_handles_json_string_metadata(self, service, mock_pipeline):
        """Should parse JSON string metadata."""
        mock_pipeline.run_steps = [
            {"metadata": json.dumps({"packages": {"pip": {"flask": "2.3.0"}}})}
        ]

        packages = service._get_packages(mock_pipeline)

        assert packages == ["flask==2.3.0"]


class TestInitializeRoarUsesExternalExecutable:
    """Test that _initialize_roar uses external roar executable."""

    @pytest.fixture
    def service(self):
        """Create EnvironmentSetupService with roar_executable."""
        svc = EnvironmentSetupService()
        svc._logger = MagicMock()
        return svc

    def test_initialize_roar_accepts_roar_executable(self, tmp_path):
        """EnvironmentSetupService should accept roar_executable parameter."""
        roar_exe = "/home/user/.venv/bin/roar"
        service = EnvironmentSetupService(roar_executable=roar_exe)

        assert service._roar_executable == roar_exe

    def test_initialize_roar_uses_external_executable(self, tmp_path):
        """_initialize_roar should use external roar executable, not venv python."""
        roar_exe = "/home/user/.venv/bin/roar"
        service = EnvironmentSetupService(roar_executable=roar_exe)

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        venv_dir = repo_dir / ".venv"
        venv_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            service._initialize_roar(repo_dir, venv_dir)

            # Should call roar init using the external executable
            mock_run.assert_called_once()
            call_args = mock_run.call_args

            # The command should use the external roar executable
            cmd = call_args[0][0]
            assert cmd[0] == roar_exe
            assert "init" in cmd
            assert "-y" in cmd

            # Should NOT use venv python
            venv_python = str(venv_dir / "bin" / "python")
            assert venv_python not in cmd


class TestGetDpkgPackages:
    """Test _get_dpkg_packages extracts dpkg packages from metadata."""

    def test_extracts_dpkg_packages_from_metadata(self, service, mock_pipeline):
        """Should extract dpkg packages from metadata.packages.dpkg."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "packages": {
                        "dpkg": {"libc6": "2.35-0ubuntu3", "libssl3": "3.0.2-0ubuntu1"}
                    }
                }
            }
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        assert packages == {"libc6": "2.35-0ubuntu3", "libssl3": "3.0.2-0ubuntu1"}

    def test_returns_versions(self, service, mock_pipeline):
        """Package dict should include versions."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "packages": {
                        "dpkg": {"curl": "7.88.1-10"}
                    }
                }
            }
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        assert packages["curl"] == "7.88.1-10"

    def test_deduplicates_across_steps(self, service, mock_pipeline):
        """Deduplicate packages from multiple steps (first occurrence wins)."""
        mock_pipeline.build_steps = [
            {"metadata": {"packages": {"dpkg": {"libc6": "2.35-0ubuntu3"}}}}
        ]
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"dpkg": {"libc6": "2.36-0ubuntu1", "curl": "7.88"}}}}
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        # First occurrence wins
        assert packages["libc6"] == "2.35-0ubuntu3"
        assert packages["curl"] == "7.88"

    def test_handles_missing_dpkg_section(self, service, mock_pipeline):
        """Return empty dict when no dpkg packages."""
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"pip": {"numpy": "1.24.1"}}}}
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        assert packages == {}

    def test_handles_none_version(self, service, mock_pipeline):
        """Should convert None version to empty string."""
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"dpkg": {"curl": None}}}}
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        assert packages == {"curl": ""}

    def test_handles_json_string_metadata(self, service, mock_pipeline):
        """Should parse JSON string metadata."""
        mock_pipeline.run_steps = [
            {"metadata": json.dumps({"packages": {"dpkg": {"curl": "7.88"}}})}
        ]

        packages = service._get_dpkg_packages(mock_pipeline)

        assert packages == {"curl": "7.88"}


class TestInstallDpkgPackages:
    """Test _install_dpkg_packages installs system packages."""

    def test_skips_on_non_debian(self, service):
        """Skip with warning on non-Debian systems."""
        with patch.object(service, "_is_debian_based", return_value=False):
            success, warnings = service._install_dpkg_packages(
                {"curl": "7.88"}, auto_confirm=True
            )

        assert success is True
        assert "non-Debian system" in warnings[0]

    def test_skips_non_interactive_needs_sudo(self, service):
        """Skip when sudo needed but non-interactive."""
        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=False),
            patch.object(service, "_is_interactive", return_value=False),
        ):
            success, warnings = service._install_dpkg_packages(
                {"curl": "7.88"}, auto_confirm=False
            )

        assert success is True
        assert "non-interactive" in warnings[0]

    def test_uses_sudo_when_not_root(self, service):
        """Prefix command with sudo when not root."""
        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=False),
            patch.object(service, "_is_interactive", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            service._install_dpkg_packages(
                {"curl": "7.88"}, auto_confirm=True
            )

            cmd = mock_run.call_args_list[0][0][0]
            assert cmd[0] == "sudo"
            assert "apt-get" in cmd

    def test_no_sudo_when_root(self, service):
        """No sudo when running as root."""
        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            service._install_dpkg_packages(
                {"curl": "7.88"}, auto_confirm=True
            )

            cmd = mock_run.call_args_list[0][0][0]
            assert cmd[0] != "sudo"
            assert cmd[0] == "apt-get"

    def test_handles_package_not_found(self, service):
        """Warn but continue when package not found."""
        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            # First call (batch install) fails
            fail_result = MagicMock(returncode=1, stderr="E: Unable to locate package")
            # Dry-run check for individual packages also fails
            mock_run.return_value = fail_result

            success, warnings = service._install_dpkg_packages(
                {"nonexistent": "1.0"}, auto_confirm=True, dpkg_any_version=True
            )

            assert success is True
            # Should have warnings about failures
            assert len(warnings) > 0

    def test_falls_back_to_any_version_with_flag(self, service):
        """With dpkg_any_version=True, install without version pin on failure."""
        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            fail = MagicMock(returncode=1, stderr="version not found")
            ok = MagicMock(returncode=0)
            # Batch versioned install fails, dry-run fails, fallback succeeds
            mock_run.side_effect = [fail, fail, ok]

            success, warnings = service._install_dpkg_packages(
                {"curl": "99.99"}, auto_confirm=True, dpkg_any_version=True
            )

            assert success is True
            # Should have installed any version
            assert any("any version" in w for w in warnings)

    def test_prompts_user_for_fallback_when_version_unavailable(self, service):
        """When exact version fails, prompt user to install any version."""
        service._presenter = MagicMock()
        service._presenter.confirm.return_value = True

        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            fail = MagicMock(returncode=1, stderr="version not found")
            ok = MagicMock(returncode=0)
            mock_run.side_effect = [fail, fail, ok]

            success, warnings = service._install_dpkg_packages(
                {"curl": "99.99"}, auto_confirm=False
            )

            # Should have prompted for fallback
            service._presenter.confirm.assert_any_call(
                "Install system packages?", default=False
            )

    def test_skips_failed_packages_when_user_declines_fallback(self, service):
        """When user declines, skip the failed packages with warning."""
        service._presenter = MagicMock()
        # First confirm (install system packages?) -> True
        # Second confirm (install available versions?) -> False
        service._presenter.confirm.side_effect = [True, False]

        with (
            patch.object(service, "_is_debian_based", return_value=True),
            patch.object(service, "_is_root", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            fail = MagicMock(returncode=1, stderr="version not found")
            mock_run.return_value = fail

            success, warnings = service._install_dpkg_packages(
                {"curl": "99.99"}, auto_confirm=False
            )

            assert success is True
            assert any("exact version not found" in w for w in warnings)


class TestPlatformDetection:
    """Test platform detection helpers."""

    def test_is_debian_based_linux(self, service):
        """Should return True on Linux with apt-get."""
        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.shutil.which", return_value="/usr/bin/apt-get"),
        ):
            assert service._is_debian_based() is True

    def test_is_not_debian_on_macos(self, service):
        """Should return False on macOS."""
        with patch("roar.services.reproduction.environment_setup.platform.system", return_value="Darwin"):
            assert service._is_debian_based() is False

    def test_is_not_debian_without_apt(self, service):
        """Should return False when apt-get is not available."""
        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.shutil.which", return_value=None),
        ):
            assert service._is_debian_based() is False


class TestEnvironmentValidation:
    """Test _validate_environment checks system compatibility."""

    def test_warns_on_os_mismatch(self, service, mock_pipeline):
        """Should warn when OS differs from original."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"}
                    }
                }
            }
        ]

        with patch("roar.services.reproduction.environment_setup.platform.system", return_value="Darwin"):
            warnings = service._validate_environment(mock_pipeline)

        assert any("OS mismatch" in w for w in warnings)

    def test_warns_on_architecture_mismatch(self, service, mock_pipeline):
        """Should warn when machine architecture differs."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"}
                    }
                }
            }
        ]

        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.platform.machine", return_value="aarch64"),
        ):
            warnings = service._validate_environment(mock_pipeline)

        assert any("Architecture mismatch" in w for w in warnings)

    def test_warns_on_cuda_version_mismatch(self, service, mock_pipeline):
        """Should warn when CUDA version differs."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"},
                        "cuda": {"cuda_version": "11.8"}
                    }
                }
            }
        ]

        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.platform.machine", return_value="x86_64"),
            patch.object(service, "_get_current_cuda_version", return_value="12.0"),
        ):
            warnings = service._validate_environment(mock_pipeline)

        assert any("CUDA version mismatch" in w for w in warnings)

    def test_warns_when_cuda_required_but_missing(self, service, mock_pipeline):
        """Should warn when original had CUDA but current doesn't."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"},
                        "cuda": {"cuda_version": "11.8"}
                    }
                }
            }
        ]

        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.platform.machine", return_value="x86_64"),
            patch.object(service, "_get_current_cuda_version", return_value=None),
        ):
            warnings = service._validate_environment(mock_pipeline)

        assert any("CUDA required" in w for w in warnings)

    def test_warns_when_gpu_required_but_missing(self, service, mock_pipeline):
        """Should warn when original had GPU but current doesn't."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"},
                        "gpu": [{"name": "NVIDIA A100"}]
                    }
                }
            }
        ]

        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.platform.machine", return_value="x86_64"),
            patch.object(service, "_check_gpu_available", return_value=False),
        ):
            warnings = service._validate_environment(mock_pipeline)

        assert any("GPU required" in w for w in warnings)

    def test_no_warnings_when_environment_matches(self, service, mock_pipeline):
        """Should return empty list when environments match."""
        mock_pipeline.run_steps = [
            {
                "metadata": {
                    "runtime": {
                        "os": {"system": "Linux", "machine": "x86_64"}
                    }
                }
            }
        ]

        with (
            patch("roar.services.reproduction.environment_setup.platform.system", return_value="Linux"),
            patch("roar.services.reproduction.environment_setup.platform.machine", return_value="x86_64"),
        ):
            warnings = service._validate_environment(mock_pipeline)

        assert warnings == []

    def test_handles_missing_runtime_metadata(self, service, mock_pipeline):
        """Should return empty list when no runtime metadata."""
        mock_pipeline.run_steps = [
            {"metadata": {"packages": {"pip": {"numpy": "1.24.1"}}}}
        ]

        warnings = service._validate_environment(mock_pipeline)

        assert warnings == []


class TestInstallPipPackages:
    """Test _install_packages installs pip packages with fallback."""

    @pytest.fixture
    def service(self):
        svc = EnvironmentSetupService()
        svc._logger = MagicMock()
        return svc

    def test_installs_all_packages_successfully(self, service, tmp_path):
        """Happy path: all packages install with exact versions."""
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            success, warnings = service._install_packages(
                venv_dir, ["numpy==1.24.1", "pandas==2.0.0"], repo_dir
            )

        assert success is True
        assert warnings == []

    def test_falls_back_to_any_version_with_flag(self, service, tmp_path):
        """With pip_any_version=True, retry without version pin on failure."""
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path

        with patch("subprocess.run") as mock_run:
            fail = MagicMock(returncode=1, stderr="no matching distribution")
            ok = MagicMock(returncode=0)
            # batch fails, dry-run fails, install succeeded packages (none), fallback ok
            mock_run.side_effect = [fail, fail, ok]

            success, warnings = service._install_packages(
                venv_dir, ["numpy==99.99"], repo_dir,
                pip_any_version=True,
            )

        assert success is True
        assert any("any version" in w for w in warnings)

    def test_prompts_user_for_fallback_when_version_unavailable(self, service, tmp_path):
        """When exact version fails, prompt user to install any version."""
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path
        service._presenter = MagicMock()
        service._presenter.confirm.return_value = True

        with patch("subprocess.run") as mock_run:
            fail = MagicMock(returncode=1, stderr="no matching distribution")
            ok = MagicMock(returncode=0)
            mock_run.side_effect = [fail, fail, ok]

            success, warnings = service._install_packages(
                venv_dir, ["numpy==99.99"], repo_dir,
                auto_confirm=False,
            )

        service._presenter.confirm.assert_called_once_with(
            "Install available versions instead?", default=True
        )

    def test_skips_failed_packages_when_user_declines_fallback(self, service, tmp_path):
        """When user declines, skip the failed packages with warning."""
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path
        service._presenter = MagicMock()
        service._presenter.confirm.return_value = False

        with patch("subprocess.run") as mock_run:
            fail = MagicMock(returncode=1, stderr="no matching distribution")
            mock_run.return_value = fail

            success, warnings = service._install_packages(
                venv_dir, ["numpy==99.99"], repo_dir,
                auto_confirm=False,
            )

        assert success is True
        assert any("exact version not found" in w for w in warnings)

    def test_identifies_individual_failed_packages(self, service, tmp_path):
        """Dry-run correctly identifies which packages fail."""
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path

        with patch("subprocess.run") as mock_run:
            fail = MagicMock(returncode=1, stderr="error")
            ok = MagicMock(returncode=0)
            # batch fails, dry-run: numpy ok, badpkg fails, install succeeded, fallback ok
            mock_run.side_effect = [fail, ok, fail, ok, ok]

            success, warnings = service._install_packages(
                venv_dir, ["numpy==1.24.1", "badpkg==99.99"], repo_dir,
                pip_any_version=True,
            )

        assert success is True
        # Only badpkg should be in warnings as fallback
        assert any("badpkg" in w for w in warnings)
        assert not any("numpy" in w for w in warnings)

    def test_uv_install_shows_stderr_output(self, service, tmp_path):
        """When uv is used, stderr output should be visible to the user.

        uv writes progress to stderr. With show_output=True, the code captures
        stderr via subprocess.PIPE but never displays it, so the user sees nothing.
        """
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        repo_dir = tmp_path
        service._use_uv = True
        service._presenter = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stderr="Resolved 5 packages in 50ms\nInstalled 5 packages in 200ms\n",
            )

            success, warnings = service._install_packages(
                venv_dir, ["numpy==1.24.1", "pandas==2.0.0"], repo_dir
            )

        assert success is True
        # The stderr output from uv should be displayed to the user
        calls = [str(c) for c in service._presenter.mock_calls]
        # Verify uv's stderr output was shown (it currently is not — this test should fail)
        assert any("Resolved" in c or "Installed" in c for c in calls), (
            "uv stderr output was captured but not displayed to the user"
        )
