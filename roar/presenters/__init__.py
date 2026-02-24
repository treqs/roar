"""
Output presenters for roar CLI.

Implements different output formats (console, JSON, etc.)
following the Strategy pattern.
"""

from .console import ConsolePresenter
from .dag_data_builder import DagDataBuilder
from .dag_renderer import DagRenderer
from .null import NullPresenter
from .show_renderer import ShowRenderer

__all__ = ["ConsolePresenter", "DagDataBuilder", "DagRenderer", "NullPresenter", "ShowRenderer"]
