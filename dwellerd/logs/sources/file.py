"""File log source — `tail -f` over a plain log file.

Starts at end-of-file so the daemon never replays a file's whole history on
boot. Survives rotation (inode changed -> reopen from byte 0 of the new
file) and truncation (size shrank -> seek to 0). The blocking read runs in
a thread so a write burst can't stall the event loop. Partial trailing
lines are buffered until the newline arrives.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

from .base import LogSource


class _Tailer:
    """Synchronous stateful tail. `poll()` returns complete new lines since
    the previous call. Runs in a worker thread."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._f = None
        self._ino: tuple[int, int] | None = None
        self._buf = ""

    def poll(self) -> list[str]:
        try:
            disk = os.stat(self.path)
        except OSError:
            # Gone (rotated away, not yet recreated) or unreadable. Drop the
            # handle and wait for it to come back.
            self.close()
            return []

        if self._f is None:
            self._f = open(self.path, "r", encoding="utf-8", errors="replace")
            self._ino = (disk.st_dev, disk.st_ino)
            self._f.seek(0, os.SEEK_END)   # skip history on first sighting
            return []

        lines: list[str] = []

        if (disk.st_dev, disk.st_ino) != self._ino:
            # Rotated: drain the old fd, then reopen the new file at byte 0
            # (all of its content is new to us).
            tail = self._f.read()
            if tail:
                self._buf += tail
            self._flush(lines)
            self._f.close()
            self._f = open(self.path, "r", encoding="utf-8", errors="replace")
            self._ino = (disk.st_dev, disk.st_ino)
            return lines

        if disk.st_size < self._f.tell():
            self._f.seek(0)                # truncated in place (`: > file`)

        data = self._f.read()
        if data:
            self._buf += data
            self._flush(lines)
        return lines

    def _flush(self, out: list[str]) -> None:
        parts = self._buf.split("\n")
        self._buf = parts.pop()            # trailing partial line (or "")
        out.extend(parts)

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = None
        self._ino = None


class FileLogSource(LogSource):
    type = "file"

    def __init__(
        self, name: str, path: str, pattern: str = ".+", poll_interval: float = 1.0,
    ) -> None:
        super().__init__(name=name, pattern=pattern, reconnect_delay=poll_interval)
        self.path = path
        self.poll_interval = max(0.2, float(poll_interval))

    async def stream(self) -> AsyncIterator[str]:
        tailer = _Tailer(self.path)
        try:
            while True:
                for line in await asyncio.to_thread(tailer.poll):
                    if line:
                        yield line
                await asyncio.sleep(self.poll_interval)
        finally:
            tailer.close()
