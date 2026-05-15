from __future__ import annotations

from pathlib import Path

from roar.execution.runtime.inject.support import abi_minor_version, bundled_abi_tag


def _make_inject_layout(root: Path, compiled_pkg: str, so_filename: str) -> Path:
    """Create a fake site-packages tree and return the simulated inject dir."""
    site_pkg = root / "site-packages"
    pkg_dir = site_pkg / compiled_pkg
    pkg_dir.mkdir(parents=True)
    (pkg_dir / so_filename).touch()
    inject_dir = site_pkg / "roar" / "execution" / "runtime" / "inject"
    inject_dir.mkdir(parents=True)
    return inject_dir


def test_bundled_abi_tag_finds_pydantic_core_tag(tmp_path: Path) -> None:
    inject_dir = _make_inject_layout(
        tmp_path,
        "pydantic_core",
        "_pydantic_core.cpython-313-x86_64-linux-gnu.so",
    )
    assert bundled_abi_tag(str(inject_dir)) == "cpython-313"


def test_bundled_abi_tag_finds_blake3_tag_when_pydantic_absent(tmp_path: Path) -> None:
    inject_dir = _make_inject_layout(
        tmp_path,
        "blake3",
        "_blake3.cpython-312-x86_64-linux-gnu.so",
    )
    assert bundled_abi_tag(str(inject_dir)) == "cpython-312"


def test_bundled_abi_tag_returns_none_with_no_compiled_deps(tmp_path: Path) -> None:
    site_pkg = tmp_path / "site-packages"
    inject_dir = site_pkg / "roar" / "execution" / "runtime" / "inject"
    inject_dir.mkdir(parents=True)
    assert bundled_abi_tag(str(inject_dir)) is None


def test_bundled_abi_tag_returns_none_when_site_packages_absent(tmp_path: Path) -> None:
    inject_dir = tmp_path / "some" / "other" / "layout"
    inject_dir.mkdir(parents=True)
    assert bundled_abi_tag(str(inject_dir)) is None


def test_abi_minor_version_parses_short_form() -> None:
    assert abi_minor_version("cp313") == (3, 13)


def test_abi_minor_version_parses_long_form() -> None:
    assert abi_minor_version("cpython-312") == (3, 12)


def test_abi_minor_version_handles_missing_or_unparseable() -> None:
    assert abi_minor_version(None) is None
    assert abi_minor_version("") is None
    assert abi_minor_version("nothing") is None
    assert abi_minor_version("cp3") is None  # too few digits to split
