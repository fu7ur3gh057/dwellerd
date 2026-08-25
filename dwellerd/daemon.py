"""The daemon: a scheduler, a log processor and a report loop in one
asyncio process.

There is no task queue and no broker. Each check gets one task that runs a
`sleep -> run -> compare -> maybe alert` loop; the log processor gets one
task per source; the report and the prune each get one. That is the entire
concurrency model, and it fits in a systemd unit with `Restart=always`.

Shutdown is deliberate: SIGTERM sets an event, every loop is cancelled, the
log sources kill their child processes in their `finally` blocks, and the
last thing that happens is one "monitoring stopped" message — so an
operator can tell a clean restart from a crash.
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import time

from .checks import build_checks
from .config import Config
from .logs import LogProcessor, build_sources
from .notify import Alert, TelegramNotifier
from .report import ReportBuilder
from .state import decide_transition
from .storage import Storage

log = logging.getLogger("dwellerd")

# How long a check may run before we give up on it. Without this a hung
# `docker ps` on a sick host would stall that check's loop forever.
_CHECK_TIMEOUT = 60.0
_PRUNE_INTERVAL = 3600.0


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.storage = Storage(cfg.db)
        self.notifier: TelegramNotifier | None = None
        if cfg.telegram.configured:
            self.notifier = TelegramNotifier(
                bot_token=cfg.telegram.bot_token,
                chat_id=cfg.telegram.chat_id,
                lang=cfg.telegram.lang,
                proxy=cfg.telegram.proxy,
                hostname=cfg.hostname,
            )
        else:
            log.warning(
                "telegram is not configured — alerts will only go to the log. "
                "Run `dwellerd setup` to add a bot token and chat id."
            )
        self.checks = build_checks(cfg)
        self.processor: LogProcessor | None = None
        self.report: ReportBuilder | None = None
        self._stop = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.storage.connect()
        self._install_signal_handlers()

        tasks: list[asyncio.Task] = []
        for check in self.checks:
            tasks.append(asyncio.create_task(
                self._check_loop(check), name=f"check:{check.name}",
            ))
        log.info("scheduled %d check(s) every %ds",
                 len(self.checks), int(self.cfg.checks.interval))

        if self.cfg.logs.enabled:
            sources = build_sources(self.cfg.logs.sources)
            if sources:
                self.processor = LogProcessor(
                    storage=self.storage,
                    sources=sources,
                    digest_interval=self.cfg.logs.digest_interval,
                    level=self.cfg.logs.level,
                    notify=self.cfg.logs.notify,
                    on_first=self._on_log_first,
                    on_digest=self._on_log_digest,
                )
                tasks.append(asyncio.create_task(
                    self._supervise("logs", self.processor.run()), name="logs",
                ))

        if self.cfg.report.enabled:
            self.report = ReportBuilder(self.cfg, self.storage)
            tasks.append(asyncio.create_task(self._report_loop(), name="report"))

        tasks.append(asyncio.create_task(self._prune_loop(), name="prune"))

        if self.notifier is not None:
            await self.notifier.send_startup()

        try:
            await self._stop.wait()
        finally:
            log.info("shutting down")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.notifier is not None:
                await self.notifier.send_shutdown()
            self.storage.close()

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:      # pragma: no cover - non-unix
                pass

    async def _supervise(self, name: str, coro) -> None:
        """Run a long-lived subsystem; log a crash loudly but never let it
        take the daemon down. Checks must keep running even if the log
        pipeline dies."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s subsystem crashed — the rest keeps running", name)

    # ── check scheduling ─────────────────────────────────────────────────

    async def _check_loop(self, check) -> None:
        # Stagger the first run so twenty checks don't all fire in the same
        # millisecond after a restart.
        await asyncio.sleep(random.uniform(0, min(5.0, check.interval)))
        while True:
            started = time.monotonic()
            try:
                await self._tick(check)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("check %s failed", check.name)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, check.interval - elapsed))

    async def _tick(self, check) -> None:
        """Run one check and alert on any level transition it produced.

        A multi-result check (docker) reports one result per container;
        each gets its own name, its own stored state and therefore its own
        transitions.
        """
        if hasattr(check, "run_multi"):
            results = await asyncio.wait_for(check.run_multi(), timeout=_CHECK_TIMEOUT)
        else:
            result = await asyncio.wait_for(check.run(), timeout=_CHECK_TIMEOUT)
            results = [(check.name, result)]

        for name, result in results:
            previous = await asyncio.to_thread(self.storage.get_level, name)
            await asyncio.to_thread(
                self.storage.set_level, name, result.level, result.detail,
            )
            fire = decide_transition(previous, result.level)
            if fire is None:
                continue
            log.info("%s: %s -> %s (%s)", name, previous or "new", result.level,
                     result.detail)
            await asyncio.to_thread(
                self.storage.record_alert, name, result.level, result.kind,
                result.detail,
            )
            if self.notifier is not None:
                await self.notifier.send_alert(Alert(
                    check=name, level=result.level, detail=result.detail,
                    kind=result.kind, metrics=result.metrics,
                ))

    # ── log callbacks ────────────────────────────────────────────────────

    async def _on_log_first(self, source: str, sample: str) -> None:
        log.info("new error signature from %s: %s", source, sample[:120])
        if self.notifier is not None:
            await self.notifier.send_log_first(source, sample)

    async def _on_log_digest(self, items: list[dict], period: str) -> None:
        if not items:
            return
        log.info("log digest: %d distinct signature(s)", len(items))
        if self.notifier is not None:
            await self.notifier.send_log_digest(items, period)

    # ── periodic loops ───────────────────────────────────────────────────

    async def _report_loop(self) -> None:
        assert self.report is not None
        while True:
            await asyncio.sleep(self.cfg.report.interval)
            try:
                text = await self.report.build()
                if self.notifier is not None:
                    await self.notifier.send_text(text)
                else:
                    log.info("report:\n%s", text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("report failed")

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(_PRUNE_INTERVAL)
            try:
                by_age, by_count = await asyncio.to_thread(
                    self.storage.prune,
                    self.cfg.logs.retention_days,
                    self.cfg.logs.max_rows,
                )
                if by_age or by_count:
                    log.info("pruned %d old + %d overflow log rows", by_age, by_count)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("prune failed")


async def run(cfg: Config) -> None:
    await Daemon(cfg).run()
