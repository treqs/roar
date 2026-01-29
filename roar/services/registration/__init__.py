"""
Registration services for GLaaS integration.

These services consolidate the registration logic from put.py and coordinator.py,
following the 4-phase registration pattern:
1. Session - Register session with git context
2. Jobs - Create jobs without artifact links
3. Artifacts - Register all artifacts
4. Link Artifacts - Link job inputs/outputs to artifacts
"""

from .artifact import ArtifactRegistrationService
from .coordinator import RegistrationCoordinator
from .job import JobRegistrationService
from .session import SessionRegistrationService

__all__ = [
    "ArtifactRegistrationService",
    "JobRegistrationService",
    "RegistrationCoordinator",
    "SessionRegistrationService",
]
