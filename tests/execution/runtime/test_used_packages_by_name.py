"""P0-6: packages the workload imported by NAME are attributed even when the
loaded module's file points elsewhere (an aliasing logging shim, e.g.
``sys.modules["wandb"] = trackio``). Guarantee: no false positives — a package
is recorded only if it was actually imported *and* is installed; the tracer
itself is never attributed.
"""

from __future__ import annotations

import importlib.metadata as ilm
import json
import sys
import types

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
    get_used_packages,
)


def test_imported_name_attributes_installed_dist_despite_alias(monkeypatch):
    """`import wandb` while `wandb` is aliased to another module: the file pass
    sees no wandb file, but the name pass records the real distribution."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})
    used = get_used_packages(
        modules_files=[],  # the aliased import loaded no wandb file
        installed_packages={"wandb": "0.16.0", "trackio": "0.1.0"},
        imported_modules=["wandb"],
    )
    assert used == {"wandb": "0.16.0"}


def test_never_imported_package_is_not_added(monkeypatch):
    """No false positives: a package that was not imported is never recorded,
    even though it is installed and maps to a distribution."""
    monkeypatch.setattr(
        ilm, "packages_distributions", lambda: {"wandb": ["wandb"], "numpy": ["numpy"]}
    )
    used = get_used_packages(
        modules_files=[],
        installed_packages={"wandb": "0.16.0", "numpy": "2.0.0"},
        imported_modules=["numpy", "os", "sys"],  # wandb never imported
    )
    assert "wandb" not in used
    assert used.get("numpy") == "2.0.0"  # imported and installed -> attributed


def test_imported_but_not_installed_and_tracer_never_attributed(monkeypatch):
    """An imported name that isn't an installed dist is skipped (no unknown-name
    fallback on this path), and roar (the tracer) is never recorded as a dep."""
    monkeypatch.setattr(
        ilm, "packages_distributions", lambda: {"ghost": ["ghost"], "roar": ["roar-cli"]}
    )
    used = get_used_packages(
        modules_files=[],
        installed_packages={"roar-cli": "0.4.4"},  # 'ghost' not installed
        imported_modules=["ghost", "roar", "roar.execution.runtime"],
    )
    assert used == {}


def test_shadowed_import_recorded_through_write_log(tmp_path, monkeypatch):
    """End-to-end through the real capture path: tracking_import records the name,
    write_log runs get_used_packages, and the shadowed package lands in the log's
    used_packages — while a never-imported package would not."""
    from roar.execution.runtime.inject import tracker as tmod

    log_path = tmp_path / "log.json"

    class _Ctl:
        def handle_import(self, *args, **kwargs):
            return None

    tracker = RuntimeInjectionTracker(
        {"ROAR_LOG_FILE": str(log_path)},
        _Ctl(),
        log_file=str(log_path),
        inject_dir=str(tmp_path / "inject"),
    )
    monkeypatch.setattr(tmod, "get_installed_packages", lambda: {"wandb": "0.16.0"})
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})

    # The shim: `wandb` resolves to a stand-in module with no __file__, so the
    # file pass can't see it. The workload then imports it -> captured by name.
    monkeypatch.setitem(sys.modules, "wandb", types.ModuleType("trackio_standin"))
    tracker.tracking_import("wandb")  # the real import-capture path

    tracker.write_log()
    payload = json.loads(log_path.read_text())
    assert "wandb" in payload["imported_modules"]
    assert payload["used_packages"].get("wandb") == "0.16.0"
