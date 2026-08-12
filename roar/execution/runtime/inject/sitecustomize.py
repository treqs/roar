# ruff: noqa: E402
import atexit
import importlib.machinery
import importlib.util
import os
import sys

_RUNTIME_CACHE_COLLISIONS_ENV = "ROAR_RUNTIME_CACHE_COLLISIONS"


def _roar_runtime_cache_root() -> str:
    """``$XDG_CACHE_HOME/roar/runtime`` (default ``~/.cache/roar/runtime``).

    Inlined to match ``lazy_install.runtime_cache_root()`` — this runs before
    roar is importable, so it can't call into roar.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.abspath(os.path.join(base, "roar", "runtime"))


def _path_key(path: str) -> str:
    """Normalize a path for comparisons without changing import ordering."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path or os.curdir)))


def _runtime_pythonpath_entries() -> list[str]:
    return [
        path for path in os.environ.get("ROAR_RUNTIME_PYTHONPATH", "").split(os.pathsep) if path
    ]


def _workload_search_path() -> list[str]:
    """Return the original workload import roots, excluding Roar-owned paths."""
    roar_paths = {_path_key(path) for path in _runtime_pythonpath_entries()}
    roar_paths.update(
        _path_key(path)
        for path in os.environ.get("ROAR_RUNTIME_PYTHONPATH_ACTIVE", "").split(os.pathsep)
        if path
    )
    roar_paths.add(_path_key(os.path.dirname(os.path.abspath(__file__))))
    return [path for path in sys.path if _path_key(path) not in roar_paths]


def _top_level_import_names(paths: list[str]) -> set[str]:
    """Discover import names supplied by one or more site-packages trees."""
    names: set[str] = set()
    import_suffixes = sorted(
        {
            *importlib.machinery.SOURCE_SUFFIXES,
            *importlib.machinery.BYTECODE_SUFFIXES,
            *importlib.machinery.EXTENSION_SUFFIXES,
        },
        key=len,
        reverse=True,
    )
    for path in paths:
        try:
            entries = os.scandir(path)
        except OSError:
            continue
        with entries:
            for entry in entries:
                entry_name = entry.name
                if entry_name.startswith(".") or entry_name == "__pycache__":
                    continue
                if entry_name.endswith((".dist-info", ".egg-info", ".data")):
                    continue
                try:
                    if entry.is_dir():
                        candidate = entry_name
                    elif entry.is_file():
                        candidate = ""
                        for suffix in import_suffixes:
                            if entry_name.endswith(suffix):
                                candidate = entry_name[: -len(suffix)]
                                break
                    else:
                        continue
                except OSError:
                    continue
                if candidate.isidentifier():
                    names.add(candidate)
    return names


def _runtime_cache_collisions(cache_paths: list[str]) -> tuple[str, ...]:
    """Names a cache would shadow on the workload's unmodified search path."""
    workload_paths = _workload_search_path()
    collisions: list[str] = []
    for name in sorted(_top_level_import_names(cache_paths)):
        try:
            spec = importlib.machinery.PathFinder.find_spec(name, workload_paths)
        except Exception:
            # Detection uncertainty must degrade rather than risk changing the workload.
            collisions.append(name)
            continue
        if spec is not None:
            collisions.append(name)
    return tuple(collisions)


def _record_runtime_cache_collisions(collisions: tuple[str, ...]) -> None:
    if collisions:
        os.environ[_RUNTIME_CACHE_COLLISIONS_ENV] = ",".join(collisions)
    else:
        os.environ.pop(_RUNTIME_CACHE_COLLISIONS_ENV, None)


def _set_active_runtime_paths(paths: list[str]) -> None:
    if paths:
        os.environ["ROAR_RUNTIME_PYTHONPATH_ACTIVE"] = os.pathsep.join(paths)
    else:
        os.environ.pop("ROAR_RUNTIME_PYTHONPATH_ACTIVE", None)


def _add_active_runtime_path(path: str, *, prepend: bool = False) -> None:
    active = [
        entry
        for entry in os.environ.get("ROAR_RUNTIME_PYTHONPATH_ACTIVE", "").split(os.pathsep)
        if entry
    ]
    if path in active:
        return
    if prepend:
        active.insert(0, path)
    else:
        active.append(path)
    _set_active_runtime_paths(active)


def _add_roar_runtime_pythonpath() -> None:
    """Make roar importable in the traced process **without letting roar's own
    environment shadow the workload's recorded packages**.

    ``ROAR_RUNTIME_PYTHONPATH`` carries two very different kinds of entry:

    - roar's lazy-installed **ABI-matched runtime cache**
      (``~/.cache/roar/runtime/<tag>/site-packages``). This *must* beat the
      system's wrong-ABI copies — that is why it was installed — so it is
      **prepended only when none of its import names overlap the workload**.
    - roar's package root / the parent interpreter's **site-packages**, added so
      a non-editable or cross-interpreter child can import roar at all. These are
      **appended**, so the workload's own venv always wins.

    Prepending the second kind was **P0-14**: when roar ran under a different
    interpreter than the child (e.g. roar under system 3.10, workload venv 3.12),
    its host ``dist-packages`` landed at ``sys.path[0]`` and shadowed the recorded
    pins — the run executed against host packages and could certify GREEN for the
    wrong reason. roar's core injection is pure-Python, so appending still leaves
    it importable; ABI-specific backend deps are handled separately by the runtime
    gate below.

    Inlined (not imported from roar) because this runs before roar is importable.
    """
    if importlib.util.find_spec("roar") is not None:
        return
    new_paths = [path for path in _runtime_pythonpath_entries() if path not in sys.path]
    if not new_paths:
        return
    cache_root = _roar_runtime_cache_root()
    must_win = [p for p in new_paths if os.path.abspath(p).startswith(cache_root + os.sep)]
    others = [p for p in new_paths if p not in must_win]
    collisions = _runtime_cache_collisions(must_win) if must_win else ()
    _record_runtime_cache_collisions(collisions)
    active_paths: list[str] = []
    if must_win and not collisions:
        sys.path[:0] = must_win  # ABI-matched cache must beat wrong-ABI system copies
        active_paths.extend(must_win)
    if others:
        sys.path.extend(others)  # roar's env must NOT shadow the workload's venv (P0-14)
        active_paths.extend(others)
    _set_active_runtime_paths(active_paths)


_add_roar_runtime_pythonpath()

from roar.execution.framework.runtime_imports import RuntimeImportController
from roar.execution.runtime.inject.support import (
    SuppressTracking,
    apply_runtime_gate,
    matching_compiled_pydantic_core,
)
from roar.execution.runtime.inject.tracker import RuntimeInjectionTracker

LOG_FILE = os.environ.get("ROAR_LOG_FILE")
_ROAR_INJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_import_controller = RuntimeImportController(os.environ)
_runtime_tracker = RuntimeInjectionTracker(
    os.environ,
    _runtime_import_controller,
    log_file=LOG_FILE,
    inject_dir=_ROAR_INJECT_DIR,
)
opened_files = _runtime_tracker.opened_files
imported_modules = _runtime_tracker.imported_modules
env_reads = _runtime_tracker.env_reads
tracking_open = _runtime_tracker.tracking_open
tracking_import = _runtime_tracker.tracking_import
patched_environ_get = _runtime_tracker.patched_environ_get

_runtime_tracker.install()


def _repair_runtime_in_process(expected_soabi: str) -> bool:
    """Install + prepend an ABI-matched runtime tree for *this* interpreter.

    Returns True if a matching compiled ``pydantic_core`` is reachable
    afterwards. This runs inside the real traced process, so the ABI is known
    for certain no matter how Python was launched — the launch-time prewarm
    only fires for a direct ``python`` target, so wrapper launches (torchrun,
    ``uv run``, shell scripts) repair here instead. Honors
    ``ROAR_RUNTIME_INSTALL=skip`` (via ``ensure_runtime``) and degrades quietly
    on any failure; the caller then leaves backend dispatch disabled.

    The caller must disable backend dispatch *before* invoking this: the imports
    below pass through the tracker's patched ``__import__``, which would
    otherwise trigger backend discovery (loading the Ray/OSMO plugin → importing
    the wrong-ABI ``pydantic_core`` → the very crash we are repairing). Tracking
    is suppressed so the repair's own file I/O doesn't land in workload lineage.
    """
    try:
        with SuppressTracking():
            from roar import __version__ as roar_version
            from roar.execution.runtime.lazy_install import ensure_runtime

            tree = ensure_runtime(
                target_python=sys.executable,
                target_abi=sys.implementation.cache_tag,
                bundled_abi=None,
                roar_version=roar_version,
            )
    except Exception:
        return False
    if tree is None:
        return False
    tree_str = str(tree)
    collisions = _runtime_cache_collisions([tree_str])
    _record_runtime_cache_collisions(collisions)
    if collisions:
        return False
    if tree_str not in sys.path:
        sys.path.insert(0, tree_str)
    _add_active_runtime_path(tree_str, prepend=True)
    return matching_compiled_pydantic_core(sys.path, expected_soabi)


def _runtime_gate_degrade_message(running_abi: tuple[int, int]) -> str:
    collisions = os.environ.get(_RUNTIME_CACHE_COLLISIONS_ENV, "")
    collision_message = (
        f"  Runtime cache disabled to preserve workload imports: {collisions}.\n"
        if collisions
        else ""
    )
    return (
        f"roar: no ABI-matched runtime found for Python {running_abi[0]}.{running_abi[1]}.\n"
        f"{collision_message}"
        f"  Backend integrations (Ray, OSMO) are disabled for this run.\n"
        f"  File I/O is still captured.\n"
        f"  Fix one of:\n"
        f"    - Install roar in this Python: pip install roar-cli\n"
        f"    - Reinstall roar-cli under matching Python:\n"
        f"        uv tool install --python python{running_abi[0]}.{running_abi[1]} "
        f"roar-cli --reinstall\n"
    )


if os.environ.get("ROAR_WRAP") == "1":
    _running_abi = (sys.version_info.major, sys.version_info.minor)
    _expected_soabi = f"cpython-{_running_abi[0]}{_running_abi[1]}"

    def _emit_runtime_gate_degrade() -> None:
        sys.stderr.write(_runtime_gate_degrade_message(_running_abi))

    # Backend interception (Ray, OSMO) is a primary job of the injection, so on
    # an ABI mismatch try to repair in-process before giving up on it. The
    # disable-before-repair / enable-only-on-success ordering lives in
    # apply_runtime_gate (unit-tested in test_inject_support.py).
    apply_runtime_gate(
        _runtime_import_controller,
        matched=matching_compiled_pydantic_core(sys.path, _expected_soabi),
        repair=lambda: _repair_runtime_in_process(_expected_soabi),
        on_degrade=_emit_runtime_gate_degrade,
    )


if os.environ.get("ROAR_WANDB_TO_TRACKIO"):
    # `roar run --wandb-to-trackio`: alias wandb -> trackio (or a silent no-op)
    # before the workload imports wandb. Best-effort — never break the workload.
    try:
        with SuppressTracking():
            from roar.integrations import wandb_trackio

            wandb_trackio.install()
    except Exception:
        pass

# Everything above is Roar's injection bootstrap, not workload activity. Keep a
# boundary snapshot so its Python packages and loaded shared libraries cannot
# leak into lineage merely because compatible copies exist in the workload venv.
_runtime_tracker.mark_workload_boundary()

atexit.register(_runtime_tracker.write_log)
