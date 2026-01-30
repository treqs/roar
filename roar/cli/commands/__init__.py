"""
Click command implementations for roar CLI.

This package contains the Click-based command implementations that
replace the legacy command classes. Each module corresponds to a
roar command (e.g., run.py implements 'roar run').

Commands are registered with the main CLI group via the
register_commands() function in roar.cli.
"""

# Migrated commands (native Click implementations)
from .auth import auth
from .build import build
from .config import config
from .dag import dag
from .env import env
from .init import init
from .lineage import lineage
from .log import log
from .pop import pop
from .register import register
from .reproduce import reproduce
from .reset import reset
from .run import run
from .show import show
from .status import status

# List of migrated commands for registration
MIGRATED_COMMANDS = [
    auth,
    build,
    config,
    env,
    dag,
    log,
    init,
    pop,
    lineage,
    register,
    reproduce,
    reset,
    run,
    show,
    status,
]

__all__ = [
    "MIGRATED_COMMANDS",
    "auth",
    "build",
    "config",
    "dag",
    "env",
    "init",
    "lineage",
    "log",
    "pop",
    "register",
    "reproduce",
    "reset",
    "run",
    "show",
    "status",
]
