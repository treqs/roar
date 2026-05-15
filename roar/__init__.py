"""roar - Run Observation & Artifact Registration."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__: str = _version("roar-cli")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
