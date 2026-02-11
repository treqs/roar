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
from .get import get
from .init import init
from .lineage import lineage
from .log import log
from .pop import pop
from .put import put
from .register import register
from .reproduce import reproduce
from .reset import reset
from .run import run
from .show import show
from .status import status
from .tracer import tracer

# List of migrated commands for registration
MIGRATED_COMMANDS = [
    auth,
    build,
    config,
    env,
    dag,
    get,
    log,
    init,
    pop,
    put,
    lineage,
    register,
    reproduce,
    reset,
    run,
    show,
    status,
    tracer,
]

__all__ = [
    "MIGRATED_COMMANDS",
    "auth",
    "build",
    "config",
    "dag",
    "env",
    "get",
    "init",
    "lineage",
    "log",
    "pop",
    "put",
    "register",
    "reproduce",
    "reset",
    "run",
    "show",
    "status",
    "tracer",
]
