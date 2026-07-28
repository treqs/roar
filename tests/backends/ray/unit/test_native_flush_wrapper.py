from __future__ import annotations

import asyncio
import inspect

from roar.backends.ray.roar_worker import _wrap_task_executor_for_native_flush


def test_sync_executor_stays_sync_and_returns_value() -> None:
    def executor(a: int, b: int) -> int:
        return a + b

    wrapped = _wrap_task_executor_for_native_flush(executor, function_name="add")

    assert not inspect.iscoroutinefunction(wrapped)
    assert wrapped(2, 3) == 5


def test_async_executor_stays_coroutine_function() -> None:
    """Regression: a sync wrapper around an async actor method makes Ray treat
    the un-awaited coroutine as the task result, which fails to pickle
    ("cannot pickle 'coroutine' object" — Ray Train v2 TrainController.run on
    the K8s/Ray dogfood cluster).
    """

    async def executor(a: int, b: int) -> int:
        return a * b

    wrapped = _wrap_task_executor_for_native_flush(executor, function_name="mul")

    assert inspect.iscoroutinefunction(wrapped)
    assert asyncio.run(wrapped(4, 5)) == 20


def test_async_executor_propagates_exceptions() -> None:
    async def executor() -> None:
        raise ValueError("boom")

    wrapped = _wrap_task_executor_for_native_flush(executor)

    try:
        asyncio.run(wrapped())
    except ValueError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("exception was swallowed by the wrapper")


def test_wrapping_is_idempotent() -> None:
    async def executor() -> int:
        return 1

    wrapped = _wrap_task_executor_for_native_flush(executor)
    rewrapped = _wrap_task_executor_for_native_flush(wrapped)

    assert rewrapped is wrapped
