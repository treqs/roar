"""
Reproduction mechanics for roar.

This package provides the concrete mechanics used by the application
reproduce workflow.

Services:
- ReproductionService: Pipeline lookup, environment prep, execution wiring
- EnvironmentSetupService: Git clone, venv, package installation
- PipelineExecutor: Execute pipeline steps
"""

from .environment_setup import EnvironmentSetupService
from .pipeline_executor import PipelineExecutor
from .service import ReproductionService

__all__ = [
    "EnvironmentSetupService",
    "PipelineExecutor",
    "ReproductionService",
]
