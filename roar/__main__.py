"""
Entry point for the `roar` command-line interface.

roar (Run Observation & Artifact Registration) is a local front-end to
TReqs' Lineage-as-a-Service (GLaaS). It registers data artifacts and
execution steps (jobs) in ML pipelines.

This module provides the main() entry point that delegates to the Click CLI.
"""


def main():
    """Main entry point for the roar CLI."""
    from .cli import cli

    cli()


if __name__ == "__main__":
    main()
