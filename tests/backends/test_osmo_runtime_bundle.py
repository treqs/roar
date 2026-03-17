from __future__ import annotations

import tarfile
from pathlib import Path

from roar.backends.osmo import build_osmo_runtime_bundle


def test_build_osmo_runtime_bundle_packages_roar_python_roots_and_tracer(tmp_path: Path) -> None:
    roar_package_dir = tmp_path / "runtime" / "roar"
    python_root = tmp_path / "site-packages"
    tracer_path = tmp_path / "bin" / "roar-tracer"
    output_path = tmp_path / "bundle" / "roar-osmo-runtime.tar.gz"

    (roar_package_dir / "cli").mkdir(parents=True)
    (roar_package_dir / "__main__.py").write_text("print('roar')\n", encoding="utf-8")
    (roar_package_dir / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (python_root / "click").mkdir(parents=True)
    (python_root / "click" / "__init__.py").write_text("", encoding="utf-8")
    (python_root / "yaml").mkdir(parents=True)
    (python_root / "yaml" / "__init__.py").write_text("", encoding="utf-8")
    tracer_path.parent.mkdir(parents=True)
    tracer_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    bundle = build_osmo_runtime_bundle(
        output_path=output_path,
        roar_package_dir=roar_package_dir,
        python_roots=[python_root],
        ptrace_tracer_path=tracer_path,
    )

    assert bundle.output_path == str(output_path)
    assert bundle.roar_package_dir == str(roar_package_dir.resolve())
    assert bundle.python_roots == [str(python_root.resolve())]
    assert bundle.ptrace_tracer_path == str(tracer_path.resolve())

    with tarfile.open(output_path, "r:gz") as archive:
        members = set(archive.getnames())

    assert "python/roar/__main__.py" in members
    assert "python/roar/cli/__init__.py" in members
    assert "python/site-packages/click/__init__.py" in members
    assert "python/site-packages/yaml/__init__.py" in members
    assert "bin/roar-tracer" in members
