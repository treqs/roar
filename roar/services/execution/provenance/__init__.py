"""
Provenance services package.

This package contains the refactored provenance collection system,
split into focused services following SOLID principles.
"""

from .service import ProvenanceService

__all__ = ["ProvenanceService"]
