"""wandb / mlflow -> trackio shim, installed into a traced child ([70]).

The injected ``sitecustomize`` calls :func:`install_trackio_shim` at interpreter
startup when ``ROAR_CAPTURE_TRACKIO`` is set (by ``roar run --capture-trackio``).
It aliases ``wandb`` to trackio and installs a minimal ``mlflow`` fluent-API shim,
so an *unmodified* workload's experiment logging funnels into trackio and syncs to
the configured HF Space — the experiment-tracking analyzer then records that Space
URL on the DAG (URL only; no metrics are stored).

Extracted here (rather than inline in sitecustomize) so it can be unit-tested
without the sitecustomize's module-level runtime setup. Dependency-free and
best-effort: a missing trackio, or any error, is a silent no-op.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any


def _make_mlflow_shim(trackio: Any, space: str | None) -> types.ModuleType:
    """A minimal ``mlflow`` module that funnels into trackio (metrics; params are
    best-effort no-ops — trackio's config is set at init)."""
    m = types.ModuleType("mlflow")
    state: dict = {}

    def set_experiment(name: str | None = None, **_k: Any) -> None:
        state["project"] = name

    def start_run(**_k: Any) -> Any:
        trackio.init(project=state.get("project", "mlflow"))

        class _Run:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_a: Any) -> None:
                trackio.finish()

        return _Run()

    def noop(*_a: Any, **_k: Any) -> None:
        return None

    m.__dict__.update(
        {
            "set_experiment": set_experiment,
            "start_run": start_run,
            "log_metric": lambda key, value, step=None, **k: trackio.log({key: value}, step=step),
            "log_metrics": lambda d, step=None, **k: trackio.log(dict(d), step=step),
            "log_param": noop,
            "log_params": noop,
            "set_tracking_uri": noop,
            "autolog": noop,
            "end_run": noop,
        }
    )
    return m


def install_trackio_shim(environ: Any = None) -> bool:
    """Install the wandb/mlflow -> trackio shims if ``ROAR_CAPTURE_TRACKIO`` is set.

    Returns ``True`` iff the shims were installed. Best-effort: a missing trackio
    or any error returns ``False`` and leaves the process unchanged.
    """
    env = environ if environ is not None else os.environ
    if env.get("ROAR_CAPTURE_TRACKIO", "") not in ("1", "true", "yes"):
        return False
    try:
        import trackio
    except Exception:
        return False
    space = env.get("ROAR_TRACKIO_SPACE_ID") or None
    try:
        # Every trackio run should sync to the campaign Space, even when the
        # workload's wandb/mlflow calls don't pass a space_id.
        if space:
            _orig_init = trackio.init

            def _init(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("space_id", space)
                return _orig_init(*args, **kwargs)

            trackio.init = _init

        # W&B -> trackio (drop-in). setdefault: never clobber a real prior import.
        sys.modules.setdefault("wandb", trackio)
        # MLflow -> trackio.
        if "mlflow" not in sys.modules:
            sys.modules["mlflow"] = _make_mlflow_shim(trackio, space)
        return True
    except Exception:
        return False
