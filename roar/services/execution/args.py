"""
Argument parser service for run/build commands.

Handles parsing of command-line arguments with clean data structures.
Follows SRP: only handles argument parsing.
"""

from ...config import get_hash_algorithms
from ...core.interfaces.logger import ILogger
from ...core.interfaces.run import RunArguments


class RunArgumentParser:
    """
    Parses run/build command arguments.

    Follows SRP: only handles argument parsing.
    Follows OCP: can be extended for new argument types.
    """

    def __init__(self, logger: ILogger | None = None) -> None:
        """Initialize argument parser with optional logger."""
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.container import get_container
            from ...services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def parse(self, args: list[str], job_type: str | None = None) -> RunArguments:
        """
        Parse command-line arguments.

        Args:
            args: Command-line arguments (after 'roar run' or 'roar build')
            job_type: 'build' for build commands, None for run commands

        Returns:
            RunArguments with parsed values
        """
        self.logger.debug("RunArgumentParser.parse: args=%s, job_type=%s", args, job_type)
        is_build = job_type == "build"

        # Check for help flag first
        if "-h" in args or "--help" in args:
            self.logger.debug("Help flag detected")
            return RunArguments(
                command=["--help"],  # Placeholder to pass validation
                is_build=is_build,
                show_help=True,
            )

        # Build all values in local variables first (model is immutable)
        quiet = False
        hash_only = False
        cli_hash_algorithms: list[str] = []
        remaining_args: list[str] = []
        i = 0

        while i < len(args):
            arg = args[i]

            if arg in ("--quiet", "-q"):
                quiet = True
                i += 1
            elif arg == "--hash" and i + 1 < len(args):
                cli_hash_algorithms.append(args[i + 1])
                i += 2
            elif arg == "--hash-only" and i + 1 < len(args):
                cli_hash_algorithms.append(args[i + 1])
                hash_only = True
                i += 2
            elif arg.startswith("--hash="):
                cli_hash_algorithms.append(arg.split("=", 1)[1])
                i += 1
            elif arg.startswith("--hash-only="):
                cli_hash_algorithms.append(arg.split("=", 1)[1])
                hash_only = True
                i += 1
            else:
                remaining_args.append(arg)
                i += 1

        # Check for DAG reference (@N or @BN)
        dag_reference: str | None = None
        param_overrides: dict[str, str] = {}
        command: list[str] = []

        if remaining_args and remaining_args[0].startswith("@"):
            dag_reference = remaining_args[0]
            self.logger.debug("DAG reference detected: %s", dag_reference)

            # Parse parameter overrides from remaining args
            for arg in remaining_args[1:]:
                if arg.startswith("--") and "=" in arg:
                    key, value = arg[2:].split("=", 1)
                    param_overrides[key] = value
                else:
                    # Non-override args go to command
                    command.append(arg)
        else:
            command = remaining_args

        # Get hash algorithms from config + CLI
        hash_algorithms = get_hash_algorithms(
            operation="run",
            cli_algorithms=cli_hash_algorithms if cli_hash_algorithms else None,
            hash_only=hash_only,
        )
        self.logger.debug("Hash algorithms resolved: %s (hash_only=%s)", hash_algorithms, hash_only)

        self.logger.debug(
            "Parsed: command=%s, dag_ref=%s, overrides=%s", command, dag_reference, param_overrides
        )
        # Construct model once with all final values
        return RunArguments(
            command=command if command else [""],  # Empty placeholder if no command
            quiet=quiet,
            hash_algorithms=hash_algorithms,
            hash_only=hash_only,
            dag_reference=dag_reference,
            param_overrides=param_overrides,
            show_help=False,
            is_build=is_build,
        )

    def get_help_text(self, is_build: bool = False) -> str:
        """Get help text for the command."""
        if is_build:
            return """Usage: roar build [--quiet] <command> [args...]

Run a build step with provenance tracking.
Build steps run before DAG steps during reproduction.

Use for:
  - Compiling native extensions (maturin, cargo, make)
  - Installing local packages (pip install -e .)
  - Any setup that should run before the main pipeline

Options:
  --quiet, -q    Suppress output summary

Examples:
  roar build maturin develop --release
  roar build pip install -e .
  roar build make -j4"""
        else:
            return """Usage: roar run [options] <command> [args...]
       roar run @N [--param=value ...]   # Re-run DAG node N
       roar run @BN [--param=value ...]  # Re-run build node N

Run a command with provenance tracking.

Options:
  --quiet, -q             Suppress output summary
  --hash <algo>           Add hash algorithm (can be repeated)
  --hash-only <algo>      Use only specified algorithms (skip config)

Hash algorithms: blake3 (default), sha256, sha512, md5"""
