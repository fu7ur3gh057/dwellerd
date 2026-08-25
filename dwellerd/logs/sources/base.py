"""Log source base classes.

A source's only job is to produce raw lines. Matching, dedup, storage and
notification all live in `LogProcessor` — that keeps sources dumb and
uniform.

`stream()` is an async generator. The processor wraps it in a task and
cancels that task on shutdown, so cancellation surfaces as an exception at
the current `await`/`yield`. Every streaming source therefore MUST clean up
(close fds, kill child processes) in a `finally`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

log = logging.getLogger(__name__)

# StreamReader buffer for subprocess stdout. A single line above this would
# otherwise raise LimitOverrunError; 1 MiB is generous for real logs.
_STREAM_LIMIT = 1024 * 1024


class LogSource:
    type = "base"

    def __init__(
        self, name: str, pattern: str = ".+", reconnect_delay: float = 5.0,
    ) -> None:
        self.name = name
        # Compiled here so a bad regex fails when the source is built (at
        # startup, loudly) rather than on the first line hours later.
        self.pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.reconnect_delay = max(1.0, float(reconnect_delay))

    async def stream(self) -> AsyncIterator[str]:  # pragma: no cover - abstract
        raise NotImplementedError
        yield  # makes this an async generator for type checkers

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class SubprocessLogSource(LogSource):
    """Base for sources that follow a child process' stdout — journalctl,
    docker logs. Subclasses provide `_argv()`."""

    def _argv(self) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def stream(self) -> AsyncIterator[str]:
        argv = self._argv()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge: error lines matter too
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError:
            log.error("log source %s: `%s` not found", self.name, argv[0])
            return
        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    # One line longer than the buffer. Drain a chunk and
                    # carry on rather than dying on a single giant line.
                    await proc.stdout.read(_STREAM_LIMIT)
                    continue
                if not raw:
                    break
                yield raw.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            await _terminate(proc)


async def _terminate(proc) -> None:
    """Best-effort terminate -> wait -> kill. Never raises: this runs inside
    a `finally` during task cancellation."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass
