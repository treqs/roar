from __future__ import annotations

import os
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from roar.execution.runtime import tracer_backends

_RUNTIME_PYTHON_PREFIX = "python"
_RUNTIME_SITE_PACKAGES_PREFIX = f"{_RUNTIME_PYTHON_PREFIX}/site-packages"
_RUNTIME_BIN_PREFIX = "bin"


@dataclass(frozen=True)
class OsmoRuntimeBundle:
    output_path: str
    roar_package_dir: str
    python_roots: list[str]
    ptrace_tracer_path: str


def build_osmo_runtime_bundle(
    *,
    output_path: Path,
    roar_package_dir: Path | None = None,
    python_roots: list[Path] | None = None,
    ptrace_tracer_path: Path | None = None,
) -> OsmoRuntimeBundle:
    resolved_roar_package_dir = (roar_package_dir or _default_roar_package_dir()).resolve()
    if not resolved_roar_package_dir.is_dir():
        raise ValueError(f"Roar package directory not found: {resolved_roar_package_dir}")

    resolved_python_roots = [path.resolve() for path in (python_roots or _default_python_roots())]
    if not resolved_python_roots:
        raise ValueError("no Python runtime roots were found to stage for the OSMO runtime bundle")
    for path in resolved_python_roots:
        if not path.is_dir():
            raise ValueError(f"Python runtime root is not a directory: {path}")

    resolved_ptrace_tracer = (ptrace_tracer_path or _default_ptrace_tracer_path()).resolve()
    if not resolved_ptrace_tracer.is_file():
        raise ValueError(f"ptrace tracer binary not found: {resolved_ptrace_tracer}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as archive:
        _add_directory(
            archive,
            source_dir=resolved_roar_package_dir,
            archive_prefix=f"{_RUNTIME_PYTHON_PREFIX}/roar",
        )
        for root in resolved_python_roots:
            _add_directory(
                archive,
                source_dir=root,
                archive_prefix=_RUNTIME_SITE_PACKAGES_PREFIX,
            )
        _add_file(
            archive,
            source_path=resolved_ptrace_tracer,
            archive_path=f"{_RUNTIME_BIN_PREFIX}/roar-tracer",
        )

    return OsmoRuntimeBundle(
        output_path=str(output_path),
        roar_package_dir=str(resolved_roar_package_dir),
        python_roots=[str(path) for path in resolved_python_roots],
        ptrace_tracer_path=str(resolved_ptrace_tracer),
    )


def _default_roar_package_dir() -> Path:
    import roar

    return Path(roar.__file__).resolve().parent


def _default_python_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in sys.path:
        candidate = Path(entry or ".").resolve()
        if candidate in seen or not candidate.is_dir():
            continue
        if candidate.name not in {"site-packages", "dist-packages"}:
            continue
        roots.append(candidate)
        seen.add(candidate)
    return roots


def _default_ptrace_tracer_path() -> Path:
    import roar

    package_dir = Path(roar.__file__).resolve().parent
    tracer_path = tracer_backends.find_ptrace_tracer(package_dir)
    if not tracer_path:
        raise ValueError("ptrace tracer binary could not be resolved from the local Roar environment")
    return Path(tracer_path)


def _add_directory(archive: tarfile.TarFile, *, source_dir: Path, archive_prefix: str) -> None:
    source_root = source_dir.resolve()
    for path in sorted(source_root.rglob("*")):
        if _should_skip_runtime_path(path):
            continue
        relative = path.relative_to(source_root)
        archive_path = str(Path(archive_prefix, relative.as_posix()))
        archive.add(path, arcname=archive_path, recursive=False)


def _add_file(archive: tarfile.TarFile, *, source_path: Path, archive_path: str) -> None:
    archive.add(source_path, arcname=archive_path, recursive=False)


def _should_skip_runtime_path(path: Path) -> bool:
    name = path.name
    if name == "__pycache__":
        return True
    if path.is_file() and (name.endswith(".pyc") or name.endswith(".pyo")):
        return True
    return path.is_symlink() and not os.path.exists(path)


__all__ = [
    "OsmoRuntimeBundle",
    "build_osmo_runtime_bundle",
]
