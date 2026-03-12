"""Compatibility shim for the shared Ray execution backend."""

from roar.ray.backend import RAY_EXECUTION_BACKEND

RAY_SUBMIT_BACKEND = RAY_EXECUTION_BACKEND
