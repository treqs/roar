"""Setup script with automatic Rust tracer build."""

import shutil
import subprocess
from pathlib import Path

from setuptools import setup


def find_cargo():
    """Find cargo binary, checking common locations."""
    cargo = shutil.which("cargo")
    if cargo:
        return cargo

    home = Path.home()
    candidates = [
        home / ".cargo" / "bin" / "cargo",
        Path("/usr/local/bin/cargo"),
        Path("/usr/bin/cargo"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def ensure_tracer():
    """Ensure tracer binary exists before package data is collected."""
    tracer_src = Path("tracer/Cargo.toml")
    tracer_built = Path("tracer/target/release/roar-tracer")
    tracer_dst = Path("roar/bin/roar-tracer")

    if tracer_dst.exists():
        return  # Already in place

    if not tracer_src.exists():
        return  # Not a full source checkout

    cargo = find_cargo()
    if cargo:
        print("Building roar-tracer...")
        subprocess.run(
            [cargo, "build", "--release", "--manifest-path", str(tracer_src)],
            check=True,
        )
        tracer_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tracer_built, tracer_dst)
        print(f"Copied tracer to {tracer_dst}")
    elif tracer_built.exists():
        print(f"Using pre-built tracer from {tracer_built}")
        tracer_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tracer_built, tracer_dst)
        print(f"Copied tracer to {tracer_dst}")
    else:
        print("Warning: cargo not found and no pre-built tracer binary")
        print("Run 'cargo build --release' in tracer/ directory first,")
        print("or install Rust from https://rustup.rs/")


# Ensure tracer is built/copied before setuptools collects package data
ensure_tracer()


try:
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

    class bdist_wheel(_bdist_wheel):
        def finalize_options(self):
            _bdist_wheel.finalize_options(self)
            self.root_is_pure = False

        def get_tag(self):
            python, abi = "py3", "none"
            # Use manylinux tag for PyPI compatibility
            import platform

            machine = platform.machine()
            if platform.system() == "Linux":
                plat = f"manylinux_2_17_{machine}"
            else:
                _, _, plat = _bdist_wheel.get_tag(self)
            return python, abi, plat

except ImportError:
    bdist_wheel = None

setup(
    cmdclass={"bdist_wheel": bdist_wheel} if bdist_wheel else {},
)
