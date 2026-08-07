"""P0-6: packages the workload imported by NAME are attributed even when the
loaded module's file points elsewhere (an aliasing logging shim, e.g.
``sys.modules["wandb"] = trackio``). Guarantee: no false positives — a package
is recorded only if it was actually imported *and* is installed; the tracer
itself is never attributed.
"""

from __future__ import annotations

import importlib.metadata as ilm
import json
import os
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
    # write_log writes the canonical path on its own, or a per-PID shard once the
    # P0-9 sharding change is present; read whichever it produced.
    shard = log_path.with_name(f"{log_path.name}.{os.getpid()}")
    written = log_path if log_path.exists() else shard
    payload = json.loads(written.read_text())
    assert "wandb" in payload["imported_modules"]
    assert payload["used_packages"].get("wandb") == "0.16.0"


def test_roar_is_never_recorded_in_the_freeze_via_file_pass(monkeypatch):
    """P0-11: roar records itself otherwise. A dev build would then pin
    roar-cli==X.Y.dev0, which can't resolve on reproduce. The file pass must skip
    roar just like the name pass does."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"roar": ["roar-cli"]})
    used = get_used_packages(
        modules_files=["/venv/lib/python3.12/site-packages/roar/__init__.py"],
        installed_packages={"roar-cli": "0.4.4.dev0"},
    )
    assert "roar-cli" not in used and "roar" not in used


class _FakeDist:
    def __init__(self, path):
        self._path = path

    def locate_file(self, rel=""):
        return self._path


def test_self_package_from_editable_egg_info_is_not_pinned(tmp_path, monkeypatch):
    """P0-12 (#264 regression): `pip install -e .` (or its leftover
    <pkg>.egg-info after uninstall) resolves inside the repo, so the name pass
    must not re-pin the workload's own package from PyPI."""
    repo = tmp_path / "repo"
    egg_info = repo / "mypkg.egg-info"
    egg_info.mkdir(parents=True)
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"mypkg": ["mypkg"]})
    monkeypatch.setattr(ilm, "distribution", lambda name: _FakeDist(egg_info))
    used = get_used_packages(
        modules_files=[],
        installed_packages={"mypkg": "1.0"},
        imported_modules=["mypkg"],
        workload_root=str(repo),
    )
    assert "mypkg" not in used


def test_real_site_packages_dep_outside_repo_is_still_pinned(tmp_path, monkeypatch):
    """The P0-12 skip must NOT drop a genuine dependency: a dist whose metadata
    lives outside the repo (normal site-packages install) is still recorded even
    when workload_root is set."""
    repo = tmp_path / "repo"
    repo.mkdir()
    dist_info = tmp_path / "site-packages" / "wandb-0.16.0.dist-info"
    dist_info.mkdir(parents=True)
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})
    monkeypatch.setattr(ilm, "distribution", lambda name: _FakeDist(dist_info))
    used = get_used_packages(
        modules_files=[],
        installed_packages={"wandb": "0.16.0"},
        imported_modules=["wandb"],
        workload_root=str(repo),
    )
    assert used.get("wandb") == "0.16.0"
