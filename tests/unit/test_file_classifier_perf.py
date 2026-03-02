"""
Performance and correctness tests for FileClassifier._build_package_file_map().

The old implementation called dist.files on every installed package, triggering
~49K pathlib.resolve() and ~520K lstat() calls — ~9 seconds in a Ray+torch env.

The new implementation uses packages_distributions() index and path extraction,
reducing _build_package_file_map() to <200ms regardless of package count.
"""

import os
import sys
import time


class TestBuildPackageFileMapPerf:
    """Verify _build_package_file_map() completes fast with many packages."""

    def test_build_package_file_map_under_threshold(self, tmp_path):
        """
        _build_package_file_map() must complete in <5 seconds even with many
        installed packages (previously took ~9s in a Ray+torch environment).
        Target: <2 seconds; hard limit for test: <5 seconds.
        """
        from roar.filters.files import FileClassifier

        t0 = time.perf_counter()
        FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 5000, (
            f"FileClassifier.__init__() took {elapsed_ms:.0f}ms; expected <5000ms. "
            f"Check _build_package_file_map() for dist.files iteration."
        )

    def test_build_package_file_map_does_not_build_large_file_index(self, tmp_path):
        """
        _build_package_file_map() must not build a file_path→package dict by
        iterating dist.files. The resulting _file_to_pkg must be empty — classify()
        uses path-based extraction from site-packages dirs instead.

        This is a behavioral proxy for "dist.files is never called": if the dict
        is empty, no RECORD scanning happened.
        """
        from roar.filters.files import FileClassifier

        fc = FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )
        # If dist.files were iterated for N packages with M files each,
        # _file_to_pkg would have N*M entries. It must be empty.
        assert fc._file_to_pkg == {}, (
            f"_file_to_pkg has {len(fc._file_to_pkg)} entries — dist.files was iterated. "
            "Use packages_distributions() index + path extraction instead."
        )

    def test_file_to_pkg_is_empty(self, tmp_path):
        """_file_to_pkg is intentionally empty; classify() uses path extraction."""
        from roar.filters.files import FileClassifier

        fc = FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )
        assert fc._file_to_pkg == {}, (
            "_file_to_pkg should be empty; classify() now uses path-based extraction."
        )

    def test_pkg_versions_populated(self, tmp_path):
        """pkg_versions should still contain installed package versions."""
        from roar.filters.files import FileClassifier

        fc = FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )
        # Should have at least some packages (we know pytest is installed)
        assert len(fc._pkg_versions) > 0, "pkg_versions should be populated from dist.metadata"
        # pytest is always installed in the test environment
        known = {k.lower() for k in fc._pkg_versions}
        assert "pytest" in known or "pytest" in fc._pkg_versions, (
            f"Expected 'pytest' in pkg_versions, got: {list(fc._pkg_versions)[:10]}"
        )

    def test_pkg_dist_map_populated(self, tmp_path):
        """_pkg_dist_map should be populated with packages_distributions() data."""
        from roar.filters.files import FileClassifier

        fc = FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )
        assert hasattr(fc, "_pkg_dist_map"), "_pkg_dist_map must be set by _build_package_file_map"
        assert isinstance(fc._pkg_dist_map, dict), "_pkg_dist_map must be a dict"
        assert len(fc._pkg_dist_map) > 0, "_pkg_dist_map should have entries"


class TestClassifySitePackages:
    """Verify classify() correctly identifies site-packages files without dist.files."""

    def _make_classifier(self, tmp_path):
        from roar.filters.files import FileClassifier
        return FileClassifier(
            repo_root=str(tmp_path),
            sys_prefix=sys.prefix,
            sys_base_prefix=sys.base_prefix,
        )

    def test_classify_site_packages_known_package(self, tmp_path):
        """A file from a known package in site-packages should classify as 'package'."""
        fc = self._make_classifier(tmp_path)

        # Find an actual site-packages file from pytest (always installed)
        import pytest as _pytest
        pytest_file = _pytest.__file__
        if pytest_file and os.path.exists(pytest_file):
            kind, _pkg = fc.classify(pytest_file)
            assert kind == "package", (
                f"Expected 'package' for {pytest_file}, got {kind!r}"
            )
            # Package name should be identified (not None)
            # It may be 'pytest' or normalized form

    def test_classify_non_site_packages_not_mislabeled(self, tmp_path):
        """A stdlib file should NOT be classified as 'package'."""
        fc = self._make_classifier(tmp_path)
        import json
        json_file = json.__file__
        if json_file and os.path.exists(json_file) and "site-packages" not in json_file:
            kind, _pkg = fc.classify(json_file)
            assert kind in ("stdlib", "system", "skip", "unmanaged"), (
                f"json.__file__ classified as {kind!r}; expected stdlib/system/skip/unmanaged. "
                f"Path: {json_file}"
            )
