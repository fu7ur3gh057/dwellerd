"""Alert batching — turns "ten things broke in the same minute" into one
message.

Checks run on independent timers but tick in the same rhythm, so a real
incident (a host out of memory, docker going down) fires its alerts within a
few seconds of each other. The batcher holds the first alert for a short
window, collects whatever else arrives, and hands the lot to `deliver` as one
list. A lone alert just waits out the window, which is why the window is
seconds, not minutes.

`window <= 0` switches batching off: every alert is delivered immediately.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Item = tuple  # (Alert, silent: bool)
Deliver = Callable[[list], Awaitable[None]]


class AlertBatcher:
    def __init__(self, deliver: Deliver, window: float = 5.0) -> None:
        self._deliver = deliver
        self.window = max(0.0, float(window))
        self._items: list[Item] = []
        self._timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def add(self, alert, silent: bool = False) -> None:
        if self.window <= 0:
            await self._send([(alert, silent)])
            return
        self._items.append((alert, silent))
        if self._timer is None:
            self._timer = asyncio.create_task(
                self._flush_after_window(), name="alert-batch",
            )

    async def _flush_after_window(self) -> None:
        await asyncio.sleep(self.window)
        # No await between waking and clearing the handle, so `close()` can
        # never see a timer that is already past its sleep.
        self._timer = None
        await self.flush()

    async def flush(self) -> None:
        """Deliver everything collected so far. Safe to call at any time;
        concurrent callers queue behind the lock, so a flush that finds the
        batch already taken simply returns."""
        async with self._lock:
            items, self._items = self._items, []
            if items:
                await self._send(items)

    async def close(self) -> None:
        """Stop the timer and deliver what is pending — called on shutdown
        and before a config reload, so no alert dies in the buffer."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        await self.flush()

    async def _send(self, items: list[Item]) -> None:
        try:
            await self._deliver(items)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The notifier never raises on network trouble, so this is a bug
            # in rendering. Losing one batch beats killing the daemon.
            log.exception("delivering %d alert(s) failed", len(items))
