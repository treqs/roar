from __future__ import annotations

from pathlib import Path

from roar.execution.provenance.native_package_fallback import (
    collect_native_python_packages,
)


def _install_fake_distribution(
    site_packages: Path, *, distribution: str, version: str, top_level: str
) -> Path:
    package = site_packages / top_level
    package.mkdir(parents=True)
    imported = package / "module.py"
    imported.write_text("value = 1\n", encoding="utf-8")

    metadata = site_packages / f"{distribution}-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (metadata / "top_level.txt").write_text(f"{top_level}\n", encoding="utf-8")
    (metadata / "RECORD").write_text(
        f"{top_level}/module.py,,\n{metadata.name}/METADATA,,\n",
        encoding="utf-8",
    )
    return imported


def test_recovers_exact_pin_from_traced_site_packages_read(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    imported = _install_fake_distribution(
        site_packages,
        distribution="demo-dist",
        version="2.4.1",
        top_level="demo_pkg",
    )

    assert collect_native_python_packages([str(imported)]) == {"demo-dist": "2.4.1"}


def test_supports_dist_packages_and_pyc_paths(tmp_path: Path) -> None:
    dist_packages = tmp_path / "usr" / "lib" / "python3" / "dist-packages"
    imported = _install_fake_distribution(
        dist_packages,
        distribution="system-demo",
        version="1.7",
        top_level="system_demo",
    )
    pyc = imported.parent / "__pycache__" / "module.cpython-312.pyc"

    assert collect_native_python_packages([str(pyc)]) == {"system-demo": "1.7"}


def test_ignores_metadata_and_pth_startup_reads(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    imported = _install_fake_distribution(
        site_packages,
        distribution="demo-dist",
        version="2.4.1",
        top_level="demo_pkg",
    )
    metadata = site_packages / "demo-dist-2.4.1.dist-info" / "METADATA"
    pth = site_packages / "startup.pth"

    assert imported.exists()
    assert collect_native_python_packages([str(metadata), str(pth)]) == {}
