# ruff: noqa: E402
import atexit
import importlib.util
import os
import sys


def _append_roar_runtime_pythonpath() -> None:
    if importlib.util.find_spec("roar") is not None:
        return
    appended = []
    for path in os.environ.get("ROAR_RUNTIME_PYTHONPATH", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)
            appended.append(path)
    if appended:
        os.environ["ROAR_RUNTIME_PYTHONPATH_ACTIVE"] = os.pathsep.join(appended)


_append_roar_runtime_pythonpath()

from roar.execution.framework.runtime_imports import RuntimeImportController
from roar.execution.runtime.inject.support import abi_minor_version, bundled_abi_tag
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


if os.environ.get("ROAR_WRAP") == "1":
    _bundled_abi = abi_minor_version(bundled_abi_tag(_ROAR_INJECT_DIR))
    _running_abi = (sys.version_info.major, sys.version_info.minor)
    if _bundled_abi is not None and _bundled_abi != _running_abi:
        sys.stderr.write(
            f"roar: traced Python is {_running_abi[0]}.{_running_abi[1]} but "
            f"roar-cli was installed under Python "
            f"{_bundled_abi[0]}.{_bundled_abi[1]}.\n"
            f"  Backend integrations (Ray, OSMO) are disabled for this run.\n"
            f"  File I/O is still captured.\n"
            f"  To re-enable backends, reinstall under the matching Python:\n"
            f"    uv tool install --python python{_running_abi[0]}.{_running_abi[1]} "
            f"roar-cli --force\n"
        )
        _runtime_import_controller.disable_backend_dispatch()
    else:
        _runtime_import_controller.initialize_selected_backend()


atexit.register(_runtime_tracker.write_log)
