"""P0-11 (broad) / P0-28: roar's own dependency footprint must be subtracted from
the freeze by the LOCATION it loaded from, never by package NAME.

The campaign runs roar in its own ``uv tool`` venv, ABI-matched to the workload,
so roar's deps and the workload's deps live in two different venvs but share
distribution *names* (e.g. ``typing_extensions`` is a dep of both roar's pydantic
and the workload's torch). Name-keyed subtraction (the reverted rc3 attempt)
stripped the workload's own copy — P0-28. Location-keyed subtraction removes only
what actually loaded from roar's install root, so the workload's copy survives.
"""

from __future__ import annotations

from roar.execution.runtime.inject.tracker import (
    _site_packages_top,
    get_used_packages,
    is_under_any_runtime_path,
    roar_footprint_paths,
)


def test_footprint_excluded_by_location_not_by_name(tmp_path):
    """The regression test for P0-28: a workload dependency that shares a NAME with
    a roar dependency survives, because we key on where it loaded from."""
    roar_root = tmp_path / "uv-tools" / "roar-cli" / "lib" / "python3.12" / "site-packages"
    wl_root = tmp_path / "wlvenv" / "lib" / "python3.12" / "site-packages"
    inject_dir = str(roar_root / "roar" / "execution" / "runtime" / "inject")
    wl_prefix = str(tmp_path / "wlvenv")

    # Loaded modules: roar's OWN click + typing_extensions (from roar_root), and the
    # WORKLOAD's OWN typing_extensions + torch (from wl_root). typing_extensions
    # collides on name across the two venvs — the exact P0-28 case.
    loaded = [
        str(roar_root / "click" / "__init__.py"),
        str(roar_root / "typing_extensions.py"),
        str(wl_root / "typing_extensions.py"),
        str(wl_root / "torch" / "__init__.py"),
    ]
    installed = {"click": "8.1.0", "typing_extensions": "4.16.0", "torch": "2.7.0"}
    roar_dep_names = {"click", "typing_extensions"}  # both are roar deps, by name

    # (1) NAME-keying (rc3) would drop the workload's typing_extensions as well —
    #     the false negative P0-28 reported.
    name_kept = [f for f in loaded if _site_packages_top(f) not in roar_dep_names]
    assert not any("typing_extensions" in f for f in name_kept), (
        "name-keying strips the workload's own typing_extensions — this is the P0-28 bug"
    )

    # (2) LOCATION-keying (the fix): exclude only what loaded from roar's root.
    excl = roar_footprint_paths(inject_dir, wl_prefix)
    assert excl, "isolated roar install root must be excluded"
    loc_kept = [f for f in loaded if not is_under_any_runtime_path(f, excl)]
    used = get_used_packages(loc_kept, installed)

    # (3) the workload's typing_extensions and torch survive; roar's click is gone.
    assert "typing_extensions" in used, "workload's own typing_extensions must survive"
    assert "torch" in used
    assert "click" not in used, "roar's footprint must be gone"


def test_shared_venv_is_not_location_excluded(tmp_path):
    """When roar is pip-installed in the workload's own venv, its root is under the
    interpreter prefix; path cannot tell the copies apart, so no location exclusion
    is applied and the freeze safely over-includes (never a false negative)."""
    venv = tmp_path / "venv"
    inject_dir = str(
        venv / "lib" / "python3.12" / "site-packages" / "roar" / "execution" / "runtime" / "inject"
    )
    assert roar_footprint_paths(inject_dir, str(venv)) == ()


def test_isolated_root_is_reported(tmp_path):
    """The isolated ``uv tool`` root (outside the workload prefix) is returned."""
    roar_root = tmp_path / "uv-tools" / "roar-cli" / "lib" / "python3.12" / "site-packages"
    inject_dir = str(roar_root / "roar" / "execution" / "runtime" / "inject")
    assert roar_footprint_paths(inject_dir, str(tmp_path / "wlvenv")) == (str(roar_root),)
