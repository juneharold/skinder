"""Finish synchronous work before propagating cancellation to its caller."""

import asyncio
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def wait_for_work(task: asyncio.Task[T]) -> T:
    # Cancelling to_thread's await does not stop its worker. Keep the task alive and
    # join it even after repeated cancellation, before stores can be closed.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - task.result() below propagates the worker error
            break
    if cancelled:
        # Retrieve a worker exception as well, without replacing cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def run_sync(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await wait_for_work(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


class SyncWork:
    """Keep request workers alive until application shutdown has joined them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        # Track the worker's lifetime, not the caller's, so cancellation cannot hide it from drain.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await wait_for_work(task)

    async def drain(self) -> None:
        while self._tasks:
            # Endpoint callers handle their own results and errors.
            await asyncio.gather(*(wait_for_work(task) for task in tuple(self._tasks)),
                                 return_exceptions=True)
