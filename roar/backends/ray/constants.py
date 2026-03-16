"""Shared constants for Ray lineage capture and presentation."""

RAY_TASK_COMMAND_PREFIXES = ("ray_task:",)

RAY_STEP_NOISE_COMMANDS = (
    "ray_task:unknown",
    "ray_task:__init__",
    "ray_task:shutdown",
    "ray_task:s3_proxy",
    "ray_task:s3_driver_proxy",
    "ray_task:RoarNodeAgent.__init__",
)


def is_ray_noise_command(command: str | None) -> bool:
    """Return True when a command is a known internal Ray/bootstrap noise step."""
    return str(command or "") in RAY_STEP_NOISE_COMMANDS
