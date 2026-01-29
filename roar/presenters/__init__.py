"""
Output presenters for roar CLI.

Implements different output formats (console, JSON, etc.)
following the Strategy pattern.
"""

from .console import ConsolePresenter
from .dag_renderer import DagRenderer

__all__ = ["ConsolePresenter", "DagRenderer"]
