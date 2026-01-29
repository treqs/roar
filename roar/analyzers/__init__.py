"""Analyzer registry and runner."""

from .base import Analyzer

# Registry of available analyzers
_ANALYZERS: list[type[Analyzer]] = []


def register(analyzer_cls: type[Analyzer]) -> type[Analyzer]:
    """Decorator to register an analyzer."""
    _ANALYZERS.append(analyzer_cls)
    return analyzer_cls


def get_analyzers() -> list[type[Analyzer]]:
    """Get all registered analyzer classes."""
    return _ANALYZERS.copy()


def run_analyzers(context: dict, config: dict | None = None) -> dict:
    """
    Run all relevant analyzers and collect results.

    Args:
        context: Context dict passed to analyzers
        config: Config dict with analyzers section

    Returns:
        Dict mapping analyzer names to their findings
    """
    config = config or {}
    analyzers_config = config.get("analyzers", {})

    results = {}

    for analyzer_cls in _ANALYZERS:
        analyzer = analyzer_cls()

        # Check if this analyzer is enabled in config
        # Config key is analyzer.name with underscores (e.g., "experiment_tracking")
        config_key = analyzer.name
        if not analyzers_config.get(config_key, True):
            continue

        # Skip if analyzer says it's not relevant
        if not analyzer.relevant(context):
            continue

        # Run analysis
        findings = analyzer.analyze(context)
        if findings is not None:
            results[analyzer.name] = findings

    return results


# Import analyzers to trigger registration
from . import experiment_trackers  # noqa: E402, F401
