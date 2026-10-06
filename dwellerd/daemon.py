"""The daemon: a scheduler, a log processor and a report loop in one
asyncio process.

There is no task queue and no broker. Each check gets one task that runs a
`sleep -> run -> gate -> maybe alert` loop; the log processor gets one task
per source; the report and the prune each get one. That is the entire
concurrency model, and it fits in a systemd unit with `Restart=always`.

What happens between a check result and a Telegram message lives in
`gate.py` (confirmation, hysteresis, reminders) and `notify/batcher.py`
(grouping). This file wires them to the clock, the database and the
notifier, and owns the lifecycle:

  start     note whether the previous run ended uncleanly, start all tasks
  SIGHUP    re-read the config; a bad one is refused and the old one keeps
            running, a good one replaces checks, log sources and notifier
  SIGTERM   cancel every loop, deliver pending alerts, say goodbye, record
            the clean stop
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import time

import psutil

from .bot import Bot
from .checks import build_checks
from .config import Config, parse, read_raw
from .gate import GateState, advance, effective_level, reminder_due
from .logs import LogProcessor, build_sources
from .notify import Alert, TelegramNotifier
from .notify.batcher import AlertBatcher
from .report import ReportBuilder
from .state import decide_transition
from .storage import Storage
from .validate import errors, validate

log = logging.getLogger("dwellerd")

# How long a check may run before we give up on it. Without this a hung
# `docker ps` on a sick host would stall that check's loop forever.
_CHECK_TIMEOUT = 60.0
_PRUNE_INTERVAL = 3600.0
# How often "last seen alive" is written; bounds how stale the timestamp in
# an "unexpected restart" notice can be.
_HEARTBEAT_INTERVAL = 30.0


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.storage = Storage(cfg.db)
        self.notifier = self._make_notifier(cfg)
        self.checks = build_checks(cfg)
        self.batcher = AlertBatcher(self._deliver, cfg.alerts.group_window)
        self.processor: LogProcessor | None = None
        self.report: ReportBuilder | None = None
        self.bot: Bot | None = None
        self._tasks: list[asyncio.Task] = []
        self._inflight: set[asyncio.Task] = set()   # gate steps that must finish
        self._background: set[asyncio.Task] = set()
        self._reload_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    @staticmethod
    def _make_notifier(cfg: Config) -> TelegramNotifier | None:
        if cfg.telegram.configured:
            return TelegramNotifier(
                bot_token=cfg.telegram.bot_token,
                chat_id=cfg.telegram.chat_id,
                lang=cfg.telegram.lang,
                proxy=cfg.telegram.proxy,
                hostname=cfg.hostname,
                interactive=cfg.telegram.interactive,
            )
        log.warning(
            "telegram is not configured — alerts will only go to the log. "
            "Run `dwellerd setup` to add a bot token and chat id."
        )
        return None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.storage.connect()
        unclean, last_seen = await asyncio.to_thread(self._note_start)
        self._install_signal_handlers()
        self._start_tasks()

        try:
            if self.notifier is not None:
                await self.notifier.send_startup(
                    unclean=unclean,
                    last_seen_ago=max(0.0, time.time() - last_seen),
                    silent=self._quiet_now(),
                )
            await self._stop.wait()
        finally:
            log.info("shutting down")
            # Wait out a reload that is mid-flight, or it would start fresh
            # tasks on a database we are about to close.
            async with self._reload_lock:
                await self._stop_tasks()
            if self.notifier is not None:
                await self.notifier.send_shutdown(silent=self._quiet_now())
            # Only after everything above: a crash anywhere before this line
            # leaves `run_state = running`, which is the whole point.
            self.storage.set_meta(run_state="stopped", last_seen=time.time())
            self.storage.close()

    def stop(self) -> None:
        self._stop.set()

    def _note_start(self) -> tuple[str | None, float]:
        """Record that we are running and work out how the last run ended.

        Returns (unclean, last_seen) where `unclean` is None for a normal
        start, "reboot" if the host booted after we were last seen, and
        "crash" otherwise (killed, OOM, power loss on a host that was
        already up, a bug).
        """
        prev = self.storage.get_meta("run_state")
        try:
            last_seen = float(self.storage.get_meta("last_seen") or 0.0)
        except ValueError:
            last_seen = 0.0
        self.storage.set_meta(run_state="running", last_seen=time.time())
        if prev != "running":
            return None, last_seen
        try:
            rebooted = last_seen > 0 and psutil.boot_time() > last_seen
        except Exception:
            rebooted = False
        return ("reboot" if rebooted else "crash"), last_seen

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:      # pragma: no cover - non-unix
                pass
        try:
            loop.add_signal_handler(signal.SIGHUP, self._request_reload)
        except (NotImplementedError, AttributeError):   # pragma: no cover
            pass

    def _request_reload(self) -> None:
        task = asyncio.get_running_loop().create_task(self.reload(), name="reload")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ── tasks ────────────────────────────────────────────────────────────

    def _start_tasks(self) -> None:
        cfg = self.cfg
        tasks: list[asyncio.Task] = []
        for check in self.checks:
            tasks.append(asyncio.create_task(
                self._check_loop(check), name=f"check:{check.name}",
            ))
        log.info("scheduled %d check(s) every %ds",
                 len(self.checks), int(cfg.checks.interval))

        if cfg.logs.enabled:
            sources = build_sources(cfg.logs.sources)
            if sources:
                self.processor = LogProcessor(
                    storage=self.storage,
                    sources=sources,
                    digest_interval=cfg.logs.digest_interval,
                    level=cfg.logs.level,
                    notify=cfg.logs.notify,
                    on_first=self._on_log_first,
                    on_digest=self._on_log_digest,
                )
                tasks.append(asyncio.create_task(
                    self._supervise("logs", self.processor.run), name="logs",
                ))

        if self.notifier is not None and self.notifier.interactive:
            self.bot = Bot(
                self.notifier, self.storage, chat_id=cfg.telegram.chat_id,
                admins=cfg.telegram.admins, lang=cfg.telegram.lang,
            )
            tasks.append(asyncio.create_task(
                self._supervise("telegram bot", self.bot.run), name="bot",
            ))

        if cfg.report.enabled:
            self.report = ReportBuilder(cfg, self.storage)
            tasks.append(asyncio.create_task(self._report_loop(), name="report"))

        tasks.append(asyncio.create_task(self._prune_loop(), name="prune"))
        tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        self._tasks = tasks

    async def _stop_tasks(self) -> None:
        """Cancel every loop, let gate steps that already started finish,
        then deliver whatever the batcher is still holding. Used by both
        shutdown and reload."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)
        await self.batcher.close()
        self.processor = None
        self.report = None
        self.bot = None

    def _apply(self, cfg: Config) -> None:
        """Swap in a configuration. Builds everything first, so a failure
        leaves the previous values untouched."""
        notifier = self._make_notifier(cfg)
        checks = build_checks(cfg)
        batcher = AlertBatcher(self._deliver, cfg.alerts.group_window)
        self.cfg, self.notifier, self.checks, self.batcher = (
            cfg, notifier, checks, batcher,
        )

    async def _supervise(self, name: str, factory) -> None:
        """Run a long-lived subsystem; log a crash loudly but never let it
        take the daemon down. Checks must keep running even if the log
        pipeline dies. Takes the coroutine *function*, so a task cancelled
        before it ever starts does not leave a never-awaited coroutine."""
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s subsystem crashed — the rest keeps running", name)

    # ── reload ───────────────────────────────────────────────────────────

    async def reload(self) -> bool:
        """Re-read the config file (SIGHUP). Returns True if it was applied.

        A config that fails to read, validate or parse is refused and the
        running one stays in force — a typo at 3am must not take down the
        monitoring. Checks, log sources, the report and the notifier are
        rebuilt; the database, and with it every check's state, is kept, so
        nothing is re-announced. Log files are re-tailed from "now".
        """
        async with self._reload_lock:
            if self._stop.is_set():
                return False
            path = self.cfg.source_path
            if path is None:
                log.warning("reload requested but this config has no file path")
                return False
            log.info("reloading config from %s", path)
            try:
                raw, _ = await asyncio.to_thread(read_raw, path)
                problems = validate(raw)
                bad = errors(problems)
                if bad:
                    raise ValueError("\n".join(str(p) for p in bad))
                new = parse(raw, source=path)
            except Exception as e:
                log.error("config reload refused, keeping the old config: %s", e)
                await self._notice("reload_failed", str(e))
                return False

            for problem in problems:
                log.warning("config: %s", problem)
            if new.db != self.cfg.db:
                log.warning("db_path changed — that needs a restart, ignoring it")

            old = self.cfg
            await self._stop_tasks()
            try:
                self._apply(new)
                self._start_tasks()
            except Exception:
                log.exception("applying the new config failed — rolling back")
                try:
                    self._apply(old)
                    self._start_tasks()
                except Exception:
                    log.exception("rollback failed too — stopping so systemd restarts us")
                    self.stop()
                    return False
                await self._notice("reload_failed", "see the service log")
                return False

            log.info("config reloaded")
            await self._notice("reloaded")
            return True

    async def _notice(self, key: str, detail: str = "") -> None:
        if self.notifier is not None:
            await self.notifier.send_notice(key, detail, silent=self._quiet_now())

    # ── quiet hours ──────────────────────────────────────────────────────

    def _quiet_now(self) -> bool:
        quiet = self.cfg.alerts.quiet
        if quiet is None:
            return False
        t = time.localtime()
        return quiet.contains(t.tm_hour * 60 + t.tm_min)

    def _silent(self, level: str) -> bool:
        """During quiet hours everything but `crit` is delivered without a
        notification sound. Nothing is dropped, so nothing needs catching
        up on in the morning."""
        return level != "crit" and self._quiet_now()

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
        """Run one check and feed every result through the gate.

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
            # The gate step writes state and then queues the alert. If the
            # task were cancelled between those two (a reload, a shutdown)
            # the state would say "announced" while nothing was sent. So it
            # runs as its own shielded task and `_stop_tasks` waits for it.
            step = asyncio.ensure_future(self._process(name, result))
            self._inflight.add(step)
            step.add_done_callback(self._inflight.discard)
            await asyncio.shield(step)

    async def _process(self, name: str, result) -> None:
        try:
            await self._gate_step(name, result)
        except Exception:
            log.exception("gate step for %s failed", name)

    async def _gate_step(self, name: str, result) -> None:
        cfg = self.cfg.alerts
        now = time.time()

        row = await asyncio.to_thread(self.storage.get_gate, name)
        since = now
        state = GateState()
        if row is not None:
            since = row.pop("since") or now
            state = GateState(**row)

        observed = effective_level(
            result.level, result.ladder, state.confirmed, cfg.hysteresis,
        )
        new, edge = advance(state, observed, cfg.confirm)
        fire = decide_transition(*edge) if edge else None
        reminder = fire is None and reminder_due(new, observed, cfg.remind_after, now)
        level = fire or ("crit" if reminder else None)

        silenced, last_alert = new.silenced, new.last_alert
        alert: Alert | None = None
        buttons = self.notifier is not None and self.notifier.interactive

        if level is not None:
            if fire is not None:
                log.info("%s: %s -> %s (%s)", name, edge[0], fire, result.detail)
                await asyncio.to_thread(
                    self.storage.record_alert, name, fire, result.kind,
                    result.detail,
                )
            muted = await asyncio.to_thread(self.storage.is_muted, name, now)
            # A recovery for an alert that was never delivered (it was muted
            # at the time) would be a message about nothing.
            orphan_recovery = fire == "ok" and state.silenced
            if muted or orphan_recovery:
                log.info("%s: %s not sent (%s)", name, level,
                         "muted" if muted else "its alert was never delivered")
                if fire == "ok":
                    silenced = False            # the problem is over either way
                elif fire is not None:
                    silenced = True             # a muted alert nobody saw
            else:
                silenced = False
                last_alert = now if level == "crit" else None
                alert = Alert(
                    check=name, level=level, detail=result.detail,
                    kind=result.kind,
                    metrics=_metrics_for(result, level),
                    ongoing=(now - since) if reminder else 0.0,
                    target_id=(
                        await asyncio.to_thread(self.storage.target_id, name)
                        if buttons and level != "ok" else 0
                    ),
                )

        if new.confirmed != "crit":
            last_alert = None

        # A check that has not confirmed anything yet is stored as "ok", which
        # is how the gate already treats it. A NULL in the database therefore
        # only ever means "row written before the gate existed".
        await asyncio.to_thread(
            self.storage.save_gate, name, observed, result.detail,
            confirmed=new.confirmed or "ok", pending=new.pending,
            pending_count=new.pending_count, silenced=silenced,
            last_alert=last_alert,
            # An acknowledgement belongs to one episode: any announced
            # change (a recovery, an escalation) ends it.
            reset_ack=fire is not None,
        )
        if alert is not None:
            await self.batcher.add(alert, silent=self._silent(alert.level))

    async def _deliver(self, items: list) -> None:
        """Batcher callback: one alert goes out as itself, several as one
        grouped message (silent only if every one of them is)."""
        if self.notifier is None:
            for alert, _ in items:
                log.info("alert (telegram off): %s %s — %s",
                         alert.check, alert.level, alert.detail)
            return
        if len(items) == 1:
            alert, silent = items[0]
            await self.notifier.send_alert(alert, silent=silent)
            return
        await self.notifier.send_group(
            [a for a, _ in items], silent=all(s for _, s in items),
        )

    # ── log callbacks ────────────────────────────────────────────────────

    async def _on_log_first(self, source: str, sample: str) -> None:
        log.info("new error signature from %s: %s", source, sample[:120])
        if self.notifier is None:
            return
        if await asyncio.to_thread(self.storage.is_muted, f"log:{source}"):
            log.info("log:%s is muted — not sent", source)
            return
        target = 0
        if self.notifier.interactive:
            target = await asyncio.to_thread(self.storage.target_id, f"log:{source}")
        await self.notifier.send_log_first(
            source, sample, silent=self._quiet_now(), target_id=target,
        )

    async def _on_log_digest(self, items: list[dict], period: str) -> None:
        if not items:
            return
        log.info("log digest: %d distinct signature(s)", len(items))
        if self.notifier is not None:
            await self.notifier.send_log_digest(
                items, period, silent=self._quiet_now(),
            )

    # ── periodic loops ───────────────────────────────────────────────────

    async def _report_loop(self) -> None:
        assert self.report is not None
        while True:
            await asyncio.sleep(self.cfg.report.interval)
            try:
                text = await self.report.build()
                if self.notifier is not None:
                    await self.notifier.send_text(text, silent=self._quiet_now())
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

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                await asyncio.to_thread(self.storage.set_meta, last_seen=time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")


def _metrics_for(result, level: str) -> dict:
    """The result's metrics, with the threshold matching the level being
    announced. A streak can confirm `warn` while the latest reading is
    already `crit`; the message must not say "warn" next to the crit
    threshold."""
    metrics = dict(result.metrics)
    if result.ladder is not None and level in ("warn", "crit") and "threshold" in metrics:
        _, warn, crit = result.ladder
        metrics["threshold"] = crit if level == "crit" else warn
    return metrics


async def run(cfg: Config) -> None:
    await Daemon(cfg).run()
