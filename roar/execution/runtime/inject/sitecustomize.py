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
from roar.execution.runtime.inject.support import matching_compiled_pydantic_core
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
    _running_abi = (sys.version_info.major, sys.version_info.minor)
    _expected_soabi = f"cpython-{_running_abi[0]}{_running_abi[1]}"
    if not matching_compiled_pydantic_core(sys.path, _expected_soabi):
        sys.stderr.write(
            f"roar: no ABI-matched runtime found for Python "
            f"{_running_abi[0]}.{_running_abi[1]}.\n"
            f"  Backend integrations (Ray, OSMO) are disabled for this run.\n"
            f"  File I/O is still captured.\n"
            f"  Fix one of:\n"
            f"    - Install roar in this Python: pip install roar-cli\n"
            f"    - Reinstall roar-cli under matching Python:\n"
            f"        uv tool install --python python{_running_abi[0]}.{_running_abi[1]} "
            f"roar-cli --reinstall\n"
        )
        _runtime_import_controller.disable_backend_dispatch()
    else:
        _runtime_import_controller.initialize_selected_backend()


atexit.register(_runtime_tracker.write_log)
