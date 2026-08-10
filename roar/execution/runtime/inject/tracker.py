"""Generic runtime-activity tracking for injected Python processes."""

from __future__ import annotations

import builtins
import contextlib
import glob
import json
import os
import platform
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, Protocol, cast

from roar.execution.runtime.inject.support import is_suppressed

_ORIGINAL_ENVIRON_GET_ATTR = "_original_get"
_ENVIRON_GET_METHOD_NAME = "get"

# roar injects its own variables (ROAR_WRAP, ROAR_EXECUTION_BACKEND,
# ROAR_RUNTIME_PYTHONPATH_ACTIVE, ...) into the traced process environment.
# roar's own injected machinery reads them back through the patched
# environ.get, which would otherwise record them as if the user's code had
# read them. They describe roar's plumbing, not the workload, so exclude this
# reserved namespace from captured lineage. See issue #164.
_ROAR_INTERNAL_ENV_PREFIX = "ROAR_"


class RuntimeImportObserver(Protocol):
    """Minimal protocol for the runtime import controller."""

    def handle_import(self, module_name: str, module: Any) -> Any:
        """Observe a module import."""


def get_loaded_shared_libs(real_open) -> list[str]:
    libs: set[str] = set()
    try:
        with real_open("/proc/self/maps", "r") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 6:
                    continue
                path = parts[5]
                if path.endswith(".so") or ".so." in path:
                    libs.add(path)
    except Exception:
        pass
    return sorted(libs)


def get_installed_packages(
    excluded_paths: Sequence[str] = (),
) -> dict[str, str]:
    packages: dict[str, str] = {}
    try:
        from importlib import metadata as importlib_metadata

        for dist in importlib_metadata.distributions():
            try:
                distribution_root = str(dist.locate_file(""))
            except Exception:
                distribution_root = ""
            if distribution_root and is_under_any_runtime_path(distribution_root, excluded_paths):
                continue
            metadata = cast(Mapping[str, str], dist.metadata)
            name = metadata.get("Name", None)
            version = metadata.get("Version", None)
            if name and version:
                # Import resolution and distributions() both follow sys.path order.
                # Preserve the first visible workload distribution for duplicate names.
                packages.setdefault(name, version)
    except Exception:
        pass
    return packages


def _dist_is_in_repo(dist_name: str, repo_root: str) -> bool:
    """True if ``dist_name``'s metadata resolves inside ``repo_root`` — i.e. it is
    the workload's OWN package (an editable ``pip install -e .``, or the leftover
    ``<pkg>.egg-info`` a later ``pip uninstall`` doesn't remove), not a real
    third-party dependency installed under site-packages."""
    try:
        from importlib import metadata as importlib_metadata

        dist = importlib_metadata.distribution(dist_name)
        path = getattr(dist, "_path", None) or dist.locate_file("")
        return os.path.abspath(str(path)).startswith(repo_root + os.sep)
    except Exception:
        return False


# Package install roots. "dist-packages" (Debian/Ubuntu system Python, e.g. the
# cert AMIs) must be recognized alongside "site-packages" — otherwise packages
# installed there are dropped from the freeze entirely (P0-18): the workload
# imports them, but the file pass ignored them and the aliased-only name pass
# (P0-13 fix) doesn't rescue a normally-loaded import.
_PACKAGE_ROOT_MARKERS = ("site-packages/", "dist-packages/")


# Standard-library / builtin top-level names never belong in a package freeze,
# and must not be attributed as roar-only (which would let a stdlib name roar
# imported shadow a same-named workload package). Bootstrap-safe: available since
# the interpreter starts.
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", frozenset()))


def _normalize_dist(name: str) -> str:
    return name.lower().replace("_", "-")


def _req_dist_name(requirement: str) -> str:
    """The distribution name at the head of a Requires-Dist string, e.g.
    ``pydantic>=2.0.0`` -> ``pydantic``; ``click (>=8.1)`` -> ``click``."""
    head = requirement.strip()
    for sep in (";", " ", "[", "(", "<", ">", "=", "!", "~"):
        idx = head.find(sep)
        if idx >= 0:
            head = head[:idx]
    return head.strip()


def roar_dependency_closure(root: str = "roar-cli") -> set[str]:
    """Transitive runtime-dependency distribution names of ``root`` (normalized),
    read from installed metadata. Extras-gated (dev/test) deps are skipped so the
    freeze isn't scrubbed of a package that only *shares a name* with a dev extra.
    Best-effort: returns an empty set on any failure, so exclusion degrades to the
    frame/baseline signals rather than dropping anything unexpectedly."""
    try:
        from importlib import metadata as importlib_metadata
    except Exception:
        return set()
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = _normalize_dist(stack.pop())
        if name in seen:
            continue
        seen.add(name)
        try:
            requirements = importlib_metadata.requires(name) or []
        except Exception:
            continue
        for requirement in requirements:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            dep = _req_dist_name(requirement)
            if dep:
                stack.append(dep)
    seen.discard(_normalize_dist(root))
    return seen


def _installed_top_names(modules: Any) -> set[str]:
    """Top-level import names of the given installed (site-/dist-packages) modules.
    ``__file__`` is used raw — ``_site_packages_top`` keys on a path substring, so
    no abspath is needed (that call is per-module and this runs on the hot start
    path). Used to resolve roar's bootstrap footprint from the install snapshot."""
    names: set[str] = set()
    for module in modules:
        fpath = getattr(module, "__file__", None)
        if not fpath:
            continue
        top = _site_packages_top(fpath)
        if top and top != "roar":
            names.add(top)
    return names


def _site_packages_top(fpath: str) -> str | None:
    """The top-level package dir for a file under a package install root, else None."""
    for marker in _PACKAGE_ROOT_MARKERS:
        idx = fpath.find(marker)
        if idx < 0:
            continue
        top = fpath[idx + len(marker) :].split("/")[0]
        if top.endswith(".py"):
            top = top[:-3]
        if top.startswith("_") or top.endswith((".dist-info", ".egg-info", ".so")):
            return None
        return top
    return None


def get_used_packages(
    modules_files: Sequence[str],
    installed_packages: Mapping[str, str | None],
    imported_modules: Sequence[str] = (),
    workload_root: str | None = None,
    loaded_files: Mapping[str, str] | None = None,
    roar_exclusive_names: Sequence[str] = (),
    pkg_dist_map: Mapping[str, list[str]] | None = None,
) -> dict[str, str | None]:
    used: dict[str, str | None] = {}
    repo_root = os.path.abspath(workload_root) if workload_root else None
    loaded = loaded_files or {}
    # Top-level import names that ONLY roar's own machinery imported (never the
    # workload). When roar shares the workload's venv, roar's deps (pydantic,
    # click, blake3, ...) load from the same site-packages as the workload's and
    # the file pass would otherwise pin them as if the job needed them — P0-11 /
    # P0-18's "roar footprint" that makes a thin freeze rebuild by luck. A name is
    # only here when NO non-roar module imported it (see tracking_import), so a
    # package the workload genuinely uses — even one roar also imports, e.g.
    # pyyaml on a timm row — is never dropped.
    roar_exclusive = set(roar_exclusive_names)

    if pkg_dist_map is None:
        try:
            from importlib import metadata as importlib_metadata

            pkg_dist_map = importlib_metadata.packages_distributions()
        except Exception:
            pkg_dist_map = {}

    try:
        for fpath in modules_files:
            top_dir = _site_packages_top(fpath)
            if top_dir is None:
                continue
            if top_dir == "roar":
                # roar records itself otherwise. roar-cli is installed separately
                # and unpinned by _install_roar, so the pin is always redundant —
                # harmless noise on a PyPI release, but fatal on an unpublished
                # build (roar-cli==X.Y.dev0 can't resolve). P0-11.
                continue
            if top_dir in roar_exclusive:
                # A dependency only roar's own machinery imported (P0-11 broad).
                continue

            pkg_names = pkg_dist_map.get(top_dir, [])
            for pkg_name in pkg_names:
                if pkg_name in installed_packages and pkg_name not in used:
                    used[pkg_name] = installed_packages[pkg_name]

            if not pkg_names and top_dir not in used:
                used[top_dir] = None
    except Exception:
        pass

    # Recover packages the workload IMPORTED but that the file pass mis-attributed
    # because the import was ALIASED — e.g. a `sys.modules["wandb"] = trackio`
    # logging shim leaves the loaded module's __file__ pointing at trackio, so the
    # file pass records trackio and never wandb, yet the job genuinely needs wandb
    # (its dist metadata is queried; its install is required).
    #
    # Scope this strictly to the aliased case: attribute a name only when it was
    # imported AND the module actually loaded for it lives in a DIFFERENT
    # site-packages package than the name. This is precisely what the file pass
    # cannot see. It deliberately excludes:
    #   - normally-loaded imports (name == loaded package)  -> the file pass's job;
    #   - merely-probed optional imports that happen to be installed (e.g.
    #     accelerate probing `sagemaker` on a SageMaker AMI) -> not loaded as an
    #     alias, so not attributed. Attributing those poisoned the freeze with
    #     unsatisfiable substrate pins — P0-13 (#264 regression).
    # A never-imported package can never appear; the tracer's own package and the
    # workload's own (editable/self) package are excluded as well.
    try:
        for name in imported_modules:
            top = name.split(".")[0]
            if not top or top.startswith("_") or top == "roar" or top in roar_exclusive:
                continue
            loaded_file = loaded.get(top)
            if not loaded_file:
                continue  # not actually loaded (find_spec probe / lazy import)
            loaded_top = _site_packages_top(loaded_file)
            if loaded_top is None or loaded_top == top:
                continue  # loaded as itself / not under site-packages -> file pass handles it
            for pkg_name in pkg_dist_map.get(top, []):
                if pkg_name not in installed_packages or pkg_name in used:
                    continue
                # The workload's OWN package (editable / leftover .egg-info in the
                # repo) is not a third-party dep — P0-12.
                if repo_root and _dist_is_in_repo(pkg_name, repo_root):
                    continue
                used[pkg_name] = installed_packages[pkg_name]
    except Exception:
        pass

    return used


_MERGE_LIST_FIELDS = (
    "opened_files",
    "imported_modules",
    "modules_files",
    "shared_libs",
    "roar_exclusive",
)
_MERGE_DICT_FIELDS = ("used_packages", "installed_packages", "env_reads")


def merge_inject_logs(base_path: str) -> None:
    """Union per-PID inject-log shards (``{base_path}.<pid>``) into one record at
    ``base_path``.

    Every process in a traced tree writes its own shard (see
    :meth:`RuntimeInjectionTracker.write_log`). Unioning them recovers the full
    workload — packages, files and imports seen by the parent AND by any worker —
    instead of whichever process happened to write last. Set/dict activity is
    unioned; scalar identity (``argv``, ``python_version``, ...) is taken from the
    richest shard, i.e. the one that imported the most modules: multiprocessing
    workers (``python -c ...``) import a subset, the workload imports everything.

    A no-op if there are no shards (e.g. the tracer produced no report).
    """
    shards: list[tuple[str, dict]] = []
    for path in sorted(glob.glob(glob.escape(base_path) + ".*")):
        try:
            with open(path) as handle:
                shards.append((path, json.load(handle)))
        except (OSError, ValueError):
            continue
    if not shards:
        return

    # Richest shard = most imported modules -> the workload, not a worker.
    primary = max((data for _, data in shards), key=lambda d: len(d.get("modules_files") or []))
    merged: dict[str, Any] = dict(primary)

    for field in _MERGE_LIST_FIELDS:
        union: set[str] = set()
        for _, data in shards:
            union.update(data.get(field) or [])
        merged[field] = sorted(union)

    for field in _MERGE_DICT_FIELDS:
        combined: dict[str, Any] = {}
        for _, data in shards:
            for key, value in (data.get(field) or {}).items():
                # Prefer a concrete version over a None placeholder.
                if key not in combined or combined[key] is None:
                    combined[key] = value
        merged[field] = dict(sorted(combined.items()))

    with open(base_path, "w") as handle:
        json.dump(merged, handle)

    for path, _ in shards:
        with contextlib.suppress(OSError):
            os.remove(path)


def get_active_runtime_pythonpath(environ: Mapping[str, str]) -> tuple[str, ...]:
    entries: list[str] = []
    for raw_path in environ.get("ROAR_RUNTIME_PYTHONPATH_ACTIVE", "").split(os.pathsep):
        if not raw_path:
            continue
        with contextlib.suppress(Exception):
            entries.append(os.path.abspath(raw_path).rstrip(os.sep) + os.sep)
    return tuple(entries)


def is_under_any_runtime_path(path: str, runtime_paths: Sequence[str]) -> bool:
    if not runtime_paths:
        return False
    abs_path = os.path.normcase(os.path.abspath(path))
    for runtime_path in runtime_paths:
        abs_runtime_path = os.path.normcase(os.path.abspath(runtime_path))
        try:
            if os.path.commonpath([abs_path, abs_runtime_path]) == abs_runtime_path:
                return True
        except ValueError:
            continue
    return False


class RuntimeInjectionTracker:
    """Capture generic process activity for the parent-side recorder."""

    def __init__(
        self,
        environ: MutableMapping[str, str],
        runtime_import_controller: RuntimeImportObserver,
        *,
        log_file: str | None = None,
        inject_dir: str | None = None,
    ) -> None:
        self._environ = environ
        self._runtime_import_controller = runtime_import_controller
        self._real_open = builtins.open
        self._real_import = builtins.__import__
        self._original_environ_get = environ.get
        self._log_file = log_file if log_file is not None else environ.get("ROAR_LOG_FILE")
        self._inject_dir = inject_dir or os.path.dirname(os.path.abspath(__file__))
        self.opened_files: set[str] = set()
        self.imported_modules: set[str] = set()
        self.env_reads: dict[str, str] = {}
        # Import attribution by ORIGIN (the importing module's __name__): a
        # top-level name goes to workload_import_names if ANY non-roar module
        # imported it, and to roar_import_names when a roar.* module did. Their
        # difference (roar-only) is subtracted from the freeze so roar's own
        # dependency footprint doesn't masquerade as the workload's. P0-11/P0-18.
        self.workload_import_names: set[str] = set()
        self.roar_import_names: set[str] = set()
        # sys.modules keys present when install() runs — roar's own bootstrap
        # footprint (roar + the deps it imports to build the tracker, before
        # __import__ is patched, so tracking_import can't attribute them). Keys are
        # snapshotted cheaply at install; their top-level names are resolved at
        # write_log (off the hot start path).
        self._install_baseline_keys: set[str] = set()

        if not hasattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR):
            with contextlib.suppress(Exception):
                setattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR, self._environ.get)
        else:
            self._original_environ_get = getattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR)

    def install(self) -> None:
        """Patch builtins and environ access for activity capture."""
        # Everything loaded right now is roar's bootstrap footprint: the workload
        # hasn't run yet. Snapshot the keys cheaply (a set copy) before patching
        # __import__ so it covers deps roar imported to build the tracker itself.
        self._install_baseline_keys = set(sys.modules)
        builtins.open = self.tracking_open
        builtins.__import__ = self.tracking_import
        setattr(self._environ, _ENVIRON_GET_METHOD_NAME, self.patched_environ_get)

    def tracking_open(self, *args, **kwargs):
        if is_suppressed():
            return self._real_open(*args, **kwargs)
        try:
            path = args[0]
            self.opened_files.add(os.path.abspath(path))
        except Exception:
            pass
        return self._real_open(*args, **kwargs)

    def tracking_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        self.imported_modules.add(name)
        # Attribute this import by its ORIGIN. A cache hit still calls __import__
        # with the requesting module's globals, so `import yaml` issued from the
        # workload is observed here even when roar loaded yaml first at injection —
        # which is exactly what a presence/timing check on sys.modules cannot see.
        if level == 0 and name:
            top = name.split(".")[0]
            if top and not top.startswith("_") and top not in _STDLIB:
                origin = (globals or {}).get("__name__") or ""
                if origin == "roar" or origin.startswith("roar."):
                    self.roar_import_names.add(top)
                else:
                    origin_file = (globals or {}).get("__file__")
                    if origin_file and _site_packages_top(origin_file) is None:
                        # Only the workload's OWN loose code — the entry script,
                        # local modules, an editable repo — protects a name from
                        # roar-dependency exclusion. An installed package's internal
                        # import (e.g. pydantic importing itself) lives under a
                        # package root, so it can't masquerade as a workload need.
                        self.workload_import_names.add(top)
        module = self._real_import(name, globals, locals, fromlist, level)

        if self._environ.get("ROAR_WRAP") != "1":
            return module

        self._runtime_import_controller.handle_import(name, module)
        return module

    def patched_environ_get(self, key, default=None):
        if key in self._environ and not key.startswith(_ROAR_INTERNAL_ENV_PREFIX):
            self.env_reads[key] = self._environ[key]
        return self._original_environ_get(key, default)

    def write_log(self) -> None:
        if not self._log_file:
            return

        runtime_pythonpath = get_active_runtime_pythonpath(self._environ)
        modules_files = sorted(
            os.path.abspath(getattr(module, "__file__", ""))
            for module in sys.modules.values()
            if getattr(module, "__file__", None)
            and not os.path.abspath(getattr(module, "__file__", "")).startswith(self._inject_dir)
            and not is_under_any_runtime_path(
                os.path.abspath(getattr(module, "__file__", "")),
                runtime_pythonpath,
            )
        )
        # Trev's #268: exclude roar's own runtime-tree dists from the installed set.
        installed_packages = get_installed_packages(excluded_paths=runtime_pythonpath)
        # name -> loaded module file, so get_used_packages can tell an ALIASED
        # import (sys.modules[name] resolves to a different package) from a normal
        # or merely-probed one. Keyed by the sys.modules key (the import name),
        # whose __file__ may point at the alias target.
        loaded_files = {
            name: os.path.abspath(getattr(module, "__file__", ""))
            for name, module in sys.modules.items()
            if getattr(module, "__file__", None)
        }
        # roar's declared dependency closure, mapped from distribution names to the
        # import names the file pass keys on. Catches deps roar pulls in via
        # importlib (e.g. the ABI gate importing pydantic) that neither the origin
        # hook nor the install baseline can observe.
        closure_dists = roar_dependency_closure()
        try:
            from importlib import metadata as importlib_metadata

            pkg_dist_map = importlib_metadata.packages_distributions()
        except Exception:
            pkg_dist_map = {}
        closure_imports = {
            imp
            for imp, dists in pkg_dist_map.items()
            if any(_normalize_dist(d) in closure_dists for d in dists)
        }  # pkg_dist_map is reused by get_used_packages below (built once).
        # roar-only footprint = what roar imported (post-install, by origin), what
        # was already loaded at install time (bootstrap), and roar's declared
        # dependency closure — MINUS anything the workload itself imported. That
        # last subtraction is what keeps a shared dependency (e.g. pyyaml on a timm
        # row) in the freeze even though roar depends on and loaded it too.
        # P0-11/P0-18.
        install_baseline = _installed_top_names(
            sys.modules[k] for k in self._install_baseline_keys if k in sys.modules
        )
        roar_exclusive = (
            self.roar_import_names | install_baseline | closure_imports
        ) - self.workload_import_names
        used_packages = get_used_packages(
            modules_files,
            installed_packages,
            sorted(self.imported_modules),
            workload_root=os.getcwd(),
            loaded_files=loaded_files,
            roar_exclusive_names=sorted(roar_exclusive),
            pkg_dist_map=pkg_dist_map,
        )
        data = {
            "opened_files": sorted(self.opened_files),
            "imported_modules": sorted(self.imported_modules),
            "env_reads": dict(sorted(self.env_reads.items())),
            "modules_files": modules_files,
            "roar_inject_dir": self._inject_dir,
            "roar_exclusive": sorted(roar_exclusive),
            "shared_libs": get_loaded_shared_libs(self._real_open),
            "sys_prefix": sys.prefix,
            "sys_base_prefix": sys.base_prefix,
            "virtual_env": self._original_environ_get("VIRTUAL_ENV", ""),
            "argv": sys.argv,
            "installed_packages": installed_packages,
            "used_packages": used_packages,
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "pid": os.getpid(),
            "ppid": os.getppid(),
        }
        # Write to a PER-PID shard, not the shared ROAR_LOG_FILE. Every process in
        # a traced tree (litdata/DataLoader workers, HF datasets num_proc, torchrun
        # ranks, any multiprocessing spawn) inherits the same ROAR_LOG_FILE and
        # runs this at exit; opening it "w" means each truncates the others, so the
        # surviving record was whichever process wrote LAST — often a worker with a
        # subset of the imports (or none of them), not the workload. Sharding by
        # pid lets merge_inject_logs() union the full tree afterwards.
        shard_path = f"{self._log_file}.{os.getpid()}"
        with self._real_open(shard_path, "w") as handle:
            json.dump(data, handle)


__all__ = [
    "RuntimeInjectionTracker",
    "get_installed_packages",
    "get_loaded_shared_libs",
    "get_used_packages",
]
