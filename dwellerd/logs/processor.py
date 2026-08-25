"""`LogProcessor` — the running heart of the logs pipeline.

Per source it runs a consumer task that:
  1. reads raw lines from `source.stream()`,
  2. keeps only lines matching the source regex AND the global level filter,
  3. computes a dedup signature and stores the line,
  4. sends one instant "new error" the first time a signature is ever seen,
     and lets everything else roll into the periodic digest.

The design choices that make this quiet rather than spammy:
  - Storage is unconditional; only *notification* honours `logs.notify`.
    Turning Telegram off never stops capture, so the digest and the report
    stay accurate.
  - The instant alert fires once per *kind* of error, not per line, and
    first-seen state lives in SQLite so it survives restarts.
  - A burst cap: on a fresh database every line is technically first-seen,
    which without a cap is a Telegram flood. Beyond the cap lines are still
    stored and still counted — they just wait for the digest.
  - Consumers auto-restart with a per-source backoff, because docker and
    journal followers exit whenever their target restarts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Callable, Awaitable

from .signature import compute_signature

if TYPE_CHECKING:
    from ..storage import Storage
    from .sources.base import LogSource

log = logging.getLogger(__name__)

# Coarse global gate applied on top of each source's own regex. "all" means
# no extra filtering (the source pattern is authoritative).
_LEVEL_PATTERNS: dict[str, re.Pattern | None] = {
    "all": None,
    "info": re.compile(
        r"(?i)\b(info|notice|warn|warning|error|err|fatal|crit|critical|"
        r"exception|traceback|fail|failed|panic)\b"
    ),
    "warn": re.compile(
        r"(?i)\b(warn|warning|error|err|fatal|crit|critical|"
        r"exception|traceback|fail|failed|panic)\b"
    ),
    "error": re.compile(
        r"(?i)\b(error|err|fatal|crit|critical|exception|traceback|"
        r"fail|failed|panic|segfault|oom)\b"
    ),
}

_DIGEST_MAX_ITEMS = 25
_FIRST_BURST = 5            # instant alerts allowed per window
_FIRST_WINDOW_S = 300.0


class LogProcessor:
    def __init__(
        self,
        *,
        storage: "Storage",
        sources: "list[LogSource]",
        digest_interval: float = 3600.0,
        level: str = "error",
        notify: bool = True,
        on_first: Callable[[str, str], Awaitable[None]] | None = None,
        on_digest: Callable[[list[dict], str], Awaitable[None]] | None = None,
    ) -> None:
        self.storage = storage
        self.sources = {s.name: s for s in sources}
        self.digest_interval = max(60.0, float(digest_interval))
        self.level = (level or "all").lower()
        self._level_re = _LEVEL_PATTERNS.get(self.level)
        if self.level not in _LEVEL_PATTERNS:
            log.warning("unknown logs.level %r — falling back to 'all'", self.level)
        self.notify = bool(notify)
        self._on_first = on_first
        self._on_digest = on_digest

        self._tasks: dict[str, asyncio.Task] = {}
        self._digest: dict[str, dict] = {}
        self._window_start = 0.0
        self._window_count = 0
        self._window_suppressed = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start every consumer plus the digest loop and block until
        cancelled, tearing down all child tasks (and their subprocesses)
        on the way out."""
        if not self.sources:
            log.info("log processor: no sources configured")
            return
        for name, source in self.sources.items():
            self._tasks[name] = asyncio.create_task(
                self._consume(source), name=f"log-source:{name}",
            )
        digest_task = asyncio.create_task(self._digest_loop(), name="log-digest")
        log.info(
            "log processor: %d source(s), level=%s, digest every %ds",
            len(self._tasks), self.level, int(self.digest_interval),
        )
        try:
            await asyncio.gather(digest_task, *self._tasks.values())
        except asyncio.CancelledError:
            raise
        finally:
            digest_task.cancel()
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(
                digest_task, *self._tasks.values(), return_exceptions=True,
            )
            self._tasks.clear()

    # ── consumer ─────────────────────────────────────────────────────────

    async def _consume(self, source: "LogSource") -> None:
        while True:
            try:
                async for line in source.stream():
                    if not line:
                        continue
                    if not source.pattern.search(line):
                        continue
                    if self._level_re is not None and not self._level_re.search(line):
                        continue
                    await self._handle(source.name, line)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "log source %s crashed; restarting in %.0fs",
                    source.name, source.reconnect_delay,
                )
            else:
                log.debug(
                    "log source %s ended; restarting in %.0fs",
                    source.name, source.reconnect_delay,
                )
            await asyncio.sleep(source.reconnect_delay)

    async def _handle(self, source: str, line: str) -> None:
        ts = time.time()
        sig = compute_signature(line, source)
        try:
            is_first = await asyncio.to_thread(
                self.storage.record_log, source, line, sig, ts,
            )
        except Exception:
            log.exception("log store write failed for source %s", source)
            return

        entry = self._digest.get(sig)
        if entry is None:
            self._digest[sig] = {"source": source, "sample": line[:500], "count": 1}
        else:
            entry["count"] += 1

        if is_first and self.notify and self._on_first and self._allow_instant():
            try:
                await self._on_first(source, line[:1000])
            except Exception:
                log.exception("instant log alert failed")

    def _allow_instant(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= _FIRST_WINDOW_S:
            self._window_start = now
            self._window_count = 0
            self._window_suppressed = 0
        if self._window_count < _FIRST_BURST:
            self._window_count += 1
            return True
        self._window_suppressed += 1
        if self._window_suppressed == 1:
            log.warning(
                "log: more than %d new errors in %.0fs — instant alerts throttled, "
                "the rest roll into the digest",
                _FIRST_BURST, _FIRST_WINDOW_S,
            )
        return False

    # ── digest ───────────────────────────────────────────────────────────

    async def _digest_loop(self) -> None:
        while True:
            await asyncio.sleep(self.digest_interval)
            try:
                await self.flush_digest()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("digest flush failed")

    async def flush_digest(self) -> None:
        """Send the accumulated per-signature counts and reset. Resetting
        every flush is what bounds memory: the accumulator only ever holds
        the distinct signatures of one window."""
        if not self._digest:
            return
        accumulated, self._digest = self._digest, {}
        if not (self.notify and self._on_digest):
            return
        items = sorted(accumulated.values(), key=lambda v: v["count"], reverse=True)
        items = [
            {"source": v["source"], "count": v["count"], "sample": v["sample"]}
            for v in items[:_DIGEST_MAX_ITEMS]
        ]
        await self._on_digest(items, "")
