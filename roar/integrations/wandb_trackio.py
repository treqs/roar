"""Optional ``wandb`` -> ``trackio`` integration for ``roar run --wandb-to-trackio``.

Opt-in and workload-agnostic. It ships *inside* roar, so anyone who reproduces a
run gets it from ``pip install roar-cli`` — no separate package, nothing baked into
a machine image, and it never appears as an un-installable dependency in a captured
lineage.

Activated only when the env var ``ROAR_WANDB_TO_TRACKIO`` is set. The
``roar run --wandb-to-trackio`` flag sets it to ``"1"``, and roar re-emits that flag
on ``roar reproduce`` (executor + ``--script``), so the toggle travels with the
recorded command. Set the env var to ``"off"`` to force the no-op even when a Space
is configured. roar's injected child ``sitecustomize`` calls :func:`install` before
the workload imports ``wandb``.

Two modes, chosen at run time from the environment — **no extra parameters**:

* **sync** — ``ROAR_WANDB_TO_TRACKIO != "off"`` and ``TRACKIO_SPACE_ID`` is set:
  alias ``wandb`` to trackio and mirror the run to the hosted Space (needs
  ``HF_TOKEN``). Project / run name / config come from the workload's own
  ``wandb.init(...)`` call — nothing is passed through roar.
* **no-op** — ``ROAR_WANDB_TO_TRACKIO == "off"``, or no ``TRACKIO_SPACE_ID``, or
  trackio isn't importable: alias ``wandb`` to a silent stub so an *unmodified*
  wandb-instrumented repo imports and runs to completion with **no external
  tracking and no credentials**. This is what a third-party reproducer gets by
  default.
"""

from __future__ import annotations

import sys

# wandb.init kwargs that trackio.init does not accept (dropped in sync mode).
_WANDB_ONLY_INIT = (
    "entity",
    "tags",
    "reinit",
    "group",
    "job_type",
    "notes",
    "dir",
    "mode",
    "anonymous",
    "magic",
    "save_code",
    "sync_tensorboard",
    "monitor_gym",
    "settings",
    "id",
    "allow_val_change",
    "force",
    "tensorboard",
)

# wandb surface trackio lacks; replaced with warn-once no-ops so the run completes.
_WANDB_NOOP_NAMES = (
    "watch",
    "unwatch",
    "define_metric",
    "save",
    "restore",
    "sweep",
    "agent",
    "log_artifact",
    "use_artifact",
    "log_model",
    "use_model",
    "mark_preempting",
    "alert",
    "login",
)
# wandb data-type constructors that show up inside log() calls.
_WANDB_TYPE_NAMES = (
    "Image",
    "Table",
    "Artifact",
    "Video",
    "Histogram",
    "Audio",
    "Html",
    "Object3D",
    "Molecule",
    "plot",
    "Settings",
)


def _to_jsonable(v):
    """Mirror wandb's silent coercion of tensors/numpy to JSON — trackio raises.

    Reduces ``torch.Tensor`` / numpy scalars to Python scalars (and n-d arrays to
    nested lists) by duck-typing ``.item()`` / ``.tolist()`` — no torch/numpy import.
    """
    if v is None or isinstance(v, (str, bytes, bool, int, float)):
        return v
    if isinstance(v, dict):
        return {k: _to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return v.item()
        except Exception:
            pass
    tolist = getattr(v, "tolist", None)
    if callable(tolist):
        try:
            return v.tolist()
        except Exception:
            pass
    return v


def _install_trackio_alias(space_id: str) -> bool:
    """Alias ``wandb`` -> trackio, mirroring to ``space_id``. False if unavailable."""
    try:
        import trackio
    except Exception:
        sys.stderr.write("[wandb->trackio] trackio not importable; using no-op\n")
        return False

    _orig_init = trackio.init

    def init(*args, **kwargs):
        for k in _WANDB_ONLY_INIT:
            kwargs.pop(k, None)
        kwargs.setdefault("space_id", space_id)
        run = _orig_init(*args, **kwargs)
        try:
            if not hasattr(run, "summary"):
                run.summary = {}
        except Exception:
            pass
        trackio.run = run
        return run

    trackio.init = init

    _orig_log = trackio.log

    def log(*args, **kwargs):
        kwargs.pop("commit", None)
        if args and isinstance(args[0], dict):
            args = (_to_jsonable(args[0]), *args[1:])
        try:
            return _orig_log(*args, **kwargs)
        except TypeError:
            kwargs.pop("step", None)
            return _orig_log(*args, **kwargs)

    trackio.log = log

    _warned: set[str] = set()

    def _stub(name):
        def _f(*a, **k):
            if name not in _warned:
                _warned.add(name)
                sys.stderr.write(f"[wandb->trackio] wandb.{name}(...) is a no-op under trackio\n")
            return None

        return _f

    for _name in (*_WANDB_NOOP_NAMES, *_WANDB_TYPE_NAMES):
        if not hasattr(trackio, _name):
            setattr(trackio, _name, _stub(_name))
    if not hasattr(trackio, "run"):
        trackio.run = None

    sys.modules["wandb"] = trackio
    sys.stderr.write(f"[wandb->trackio] active (wandb -> trackio, space={space_id})\n")
    return True


def _install_noop_wandb() -> None:
    """Alias ``wandb`` to a silent no-op module so an unmodified repo runs untracked."""
    import importlib.machinery
    import types

    mod = types.ModuleType("wandb")
    # types.ModuleType leaves __spec__ = None, and importlib.util.find_spec RAISES
    # ("wandb.__spec__ is None") rather than returning None on that. accelerate's
    # is_wandb_available() calls find_spec at `import accelerate`, so a credential-
    # free host (i.e. every cold reproduce host) crashes on import. Give the stub a
    # real spec. P0-15.
    mod.__spec__ = importlib.machinery.ModuleSpec("wandb", loader=None)

    def _noop(*a, **k):
        return None

    class _Run:
        def __init__(self) -> None:
            self.summary: dict = {}
            self.config: dict = {}
            self.id = None
            self.name = None

        log = _noop
        finish = _noop
        watch = _noop
        log_code = _noop

    _run = _Run()

    def init(*a, **k):
        mod.run = _run  # type: ignore[attr-defined]
        return _run

    mod.init = init  # type: ignore[attr-defined]
    mod.log = _noop  # type: ignore[attr-defined]
    mod.finish = _noop  # type: ignore[attr-defined]
    mod.config = {}  # type: ignore[attr-defined]
    mod.run = None  # type: ignore[attr-defined]
    for _name in (*_WANDB_NOOP_NAMES, *_WANDB_TYPE_NAMES):
        setattr(mod, _name, _noop)
    sys.modules["wandb"] = mod
    sys.stderr.write("[wandb->trackio] wandb no-op (tracking disabled)\n")


def install(environ=None) -> None:
    """Alias ``wandb`` per ``ROAR_WANDB_TO_TRACKIO`` — a no-op if the flag is unset.

    Idempotent and safe: never raises into the workload. Skips if ``wandb`` is
    already in ``sys.modules`` (real wandb or a prior alias) so it never clobbers.
    """
    import os

    env = os.environ if environ is None else environ
    flag = (env.get("ROAR_WANDB_TO_TRACKIO") or "").strip().lower()
    if not flag:
        return
    if "wandb" in sys.modules:
        return
    space_id = env.get("TRACKIO_SPACE_ID") or None
    try:
        if flag != "off" and space_id and _install_trackio_alias(space_id):
            return
        _install_noop_wandb()
    except Exception as exc:  # never let tracking setup break the workload
        sys.stderr.write(f"[wandb->trackio] disabled (setup error: {exc})\n")
