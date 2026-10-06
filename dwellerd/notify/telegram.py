"""Telegram notifier — sendMessage over the Bot API, HTML parse mode.

No aiogram, no polling, no commands: this daemon only ever pushes. That
makes the whole integration one POST, which means no bot process to keep
alive and nothing listening for input.

Messages are built from per-(kind, level) templates so an alert reads like
a sentence a human wrote, not a metrics dump. Anything without a template
falls back to the check's own `detail` line.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..i18n import fmt_duration, normalize_lang
from .base import Alert

log = logging.getLogger(__name__)

_LEVEL_ICON = {"ok": "✅", "warn": "🟡", "crit": "🔴"}

# Telegram hard-limits a message to 4096 characters.
_MAX_LEN = 4000

_LABELS = {
    "en": {
        "startup": "Monitoring started",
        "shutdown": "Monitoring stopped",
        "log_first": "New error",
        "log_digest": "Error digest",
        "test": "Test alert",
        "group": "alerts",
        "more": "…and {n} more",
        "ongoing": "⏳ Still unresolved after <b>{duration}</b>.",
        "reboot": "⚠️ The previous run ended unexpectedly: the host was rebooted "
                  "(last seen {when}).",
        "crash": "⚠️ The previous run ended unexpectedly: the daemon was killed "
                 "or crashed (last seen {when}).",
        "ago": "{duration} ago",
        "reloaded": "Configuration reloaded",
        "reload_failed": "Configuration reload failed — still running on the old one",
        "btn_mute1": "🔕 Mute 1h",
        "btn_mute8": "🔕 Mute 8h",
        "btn_ack": "👍 Got it",
    },
    "ru": {
        "startup": "Мониторинг запущен",
        "shutdown": "Мониторинг остановлен",
        "log_first": "Новая ошибка",
        "log_digest": "Дайджест ошибок",
        "test": "Тестовый алерт",
        "group": "алертов",
        "more": "…и ещё {n}",
        "ongoing": "⏳ Проблема не решена уже <b>{duration}</b>.",
        "reboot": "⚠️ Прошлый запуск завершился неожиданно: хост был перезагружен "
                  "(последняя активность {when}).",
        "crash": "⚠️ Прошлый запуск завершился неожиданно: демон был убит "
                 "или упал (последняя активность {when}).",
        "ago": "{duration} назад",
        "reloaded": "Конфигурация перезагружена",
        "reload_failed": "Не удалось перезагрузить конфигурацию — работаю на старой",
        "btn_mute1": "🔕 Тихо 1ч",
        "btn_mute8": "🔕 Тихо 8ч",
        "btn_ack": "👍 Принял",
    },
}

_TITLES = {
    "en": {
        ("cpu", "warn"): "CPU usage elevated",
        ("cpu", "crit"): "CPU usage critical",
        ("cpu", "ok"): "CPU back to normal",
        ("memory", "warn"): "Memory usage elevated",
        ("memory", "crit"): "Memory usage critical",
        ("memory", "ok"): "Memory back to normal",
        ("swap", "warn"): "Swap usage elevated",
        ("swap", "crit"): "Swap usage critical",
        ("swap", "ok"): "Swap back to normal",
        ("load", "warn"): "Load average elevated",
        ("load", "crit"): "Load average critical",
        ("load", "ok"): "Load average back to normal",
        ("disk", "warn"): "Disk space running low",
        ("disk", "crit"): "Disk space critical",
        ("disk", "ok"): "Disk space recovered",
        ("http", "warn"): "HTTP check warning",
        ("http", "crit"): "HTTP endpoint down",
        ("http", "ok"): "HTTP endpoint recovered",
        ("systemd", "warn"): "Service degraded",
        ("systemd", "crit"): "Service is down",
        ("systemd", "ok"): "Service is back up",
        ("docker", "warn"): "Container degraded",
        ("docker", "crit"): "Container is down",
        ("docker", "ok"): "Container is back up",
    },
    "ru": {
        ("cpu", "warn"): "Повышенная нагрузка на CPU",
        ("cpu", "crit"): "Критическая нагрузка на CPU",
        ("cpu", "ok"): "CPU вернулся в норму",
        ("memory", "warn"): "Повышенный расход памяти",
        ("memory", "crit"): "Критическая загрузка памяти",
        ("memory", "ok"): "Память вернулась в норму",
        ("swap", "warn"): "Повышенное использование swap",
        ("swap", "crit"): "Критическое использование swap",
        ("swap", "ok"): "Swap вернулся в норму",
        ("load", "warn"): "Повышенный load average",
        ("load", "crit"): "Критический load average",
        ("load", "ok"): "Load average вернулся в норму",
        ("disk", "warn"): "Места на диске мало",
        ("disk", "crit"): "Критически мало места на диске",
        ("disk", "ok"): "Место на диске восстановлено",
        ("http", "warn"): "Эндпоинт отвечает с предупреждениями",
        ("http", "crit"): "Эндпоинт недоступен",
        ("http", "ok"): "Эндпоинт снова доступен",
        ("systemd", "warn"): "Сервис деградировал",
        ("systemd", "crit"): "Сервис не активен",
        ("systemd", "ok"): "Сервис снова работает",
        ("docker", "warn"): "Контейнер деградировал",
        ("docker", "crit"): "Контейнер не работает",
        ("docker", "ok"): "Контейнер снова работает",
    },
}

_BODIES_FIRING = {
    "en": {
        "cpu": "CPU is at <b>{value:.1f}%</b> (threshold: <b>{threshold:.0f}%</b>).",
        "memory": "Memory is at <b>{value:.1f}%</b> (threshold: <b>{threshold:.0f}%</b>).",
        "swap": "Swap is at <b>{value:.1f}%</b> (threshold: <b>{threshold:.0f}%</b>).",
        "load": "Load average <b>{load1:.2f}</b> over <b>{cores}</b> cores "
                "= <b>{per_core:.2f}</b>/core (threshold: <b>{threshold:.1f}</b>).",
        "disk": "Partition <code>{path}</code> is at <b>{value:.1f}%</b> "
                "(threshold: <b>{threshold:.0f}%</b>, free: <b>{free_gb:.1f} GB</b>).",
        "http": "<code>{url}</code> — <b>{summary}</b>",
        "systemd": "Unit <code>{unit}</code> state: <b>{state}</b>",
        "docker": "Container <code>{container}</code> is <b>{state}</b>.",
    },
    "ru": {
        "cpu": "CPU загружен на <b>{value:.1f}%</b> (порог: <b>{threshold:.0f}%</b>).",
        "memory": "Память занята на <b>{value:.1f}%</b> (порог: <b>{threshold:.0f}%</b>).",
        "swap": "Swap занят на <b>{value:.1f}%</b> (порог: <b>{threshold:.0f}%</b>).",
        "load": "Load average <b>{load1:.2f}</b> при <b>{cores}</b> ядрах "
                "= <b>{per_core:.2f}</b>/ядро (порог: <b>{threshold:.1f}</b>).",
        "disk": "Раздел <code>{path}</code> заполнен на <b>{value:.1f}%</b> "
                "(порог: <b>{threshold:.0f}%</b>, свободно: <b>{free_gb:.1f} ГБ</b>).",
        "http": "<code>{url}</code> — <b>{summary}</b>",
        "systemd": "Юнит <code>{unit}</code> в состоянии: <b>{state}</b>",
        "docker": "Контейнер <code>{container}</code> — <b>{state}</b>.",
    },
}

_BODIES_OK = {
    "en": {
        "cpu": "CPU is now at <b>{value:.1f}%</b>.",
        "memory": "Memory is now at <b>{value:.1f}%</b>.",
        "swap": "Swap is now at <b>{value:.1f}%</b>.",
        "load": "Load average is back to <b>{load1:.2f}</b> "
                "(<b>{per_core:.2f}</b>/core).",
        "disk": "Partition <code>{path}</code> is back to <b>{value:.1f}%</b> used "
                "(free: <b>{free_gb:.1f} GB</b>).",
        "http": "<code>{url}</code> is responding again — <b>{summary}</b>.",
        "systemd": "Unit <code>{unit}</code> is active again.",
        "docker": "Container <code>{container}</code> is running again.",
    },
    "ru": {
        "cpu": "CPU вернулся к <b>{value:.1f}%</b>.",
        "memory": "Память вернулась к <b>{value:.1f}%</b>.",
        "swap": "Swap вернулся к <b>{value:.1f}%</b>.",
        "load": "Load average вернулся к <b>{load1:.2f}</b> "
                "(<b>{per_core:.2f}</b>/ядро).",
        "disk": "Раздел <code>{path}</code> вернулся к <b>{value:.1f}%</b> "
                "(свободно: <b>{free_gb:.1f} ГБ</b>).",
        "http": "<code>{url}</code> снова отвечает — <b>{summary}</b>.",
        "systemd": "Юнит <code>{unit}</code> снова активен.",
        "docker": "Контейнер <code>{container}</code> снова работает.",
    },
}


def esc(s) -> str:
    """Escape for Telegram's HTML parse mode (the only three that matter)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# A grouped message adds alerts until it reaches this many characters, then
# counts the rest. Cutting a finished message at Telegram's hard limit could
# slice through a tag and get the whole send rejected, so the budget is
# enforced while building it, comfortably under `_MAX_LEN`.
_GROUP_BUDGET = 3300
_GROUP_LINE = 140


def trunc(s, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


class TelegramNotifier:
    def __init__(
        self, bot_token: str, chat_id: str, lang: str = "en",
        proxy: str = "", hostname: str = "", interactive: bool = False,
    ) -> None:
        self.token = bot_token
        self.chat_id = chat_id
        self.lang = normalize_lang(lang)
        self.proxy = proxy or None
        self.hostname = hostname
        self.interactive = interactive
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # ── message builders ─────────────────────────────────────────────────

    def _footer(self) -> str:
        # Telegram already displays the delivery time next to every message.
        return f"<i>{esc(self.hostname)}</i>" if self.hostname else ""

    def _with_footer(self, body: str) -> str:
        footer = self._footer()
        return f"{body}\n\n{footer}" if footer else body

    def _body(self, alert: Alert) -> str:
        if alert.kind and alert.metrics:
            table = _BODIES_OK if alert.level == "ok" else _BODIES_FIRING
            template = table.get(self.lang, table["en"]).get(alert.kind)
            if template:
                try:
                    return template.format(**alert.metrics)
                except (KeyError, ValueError, IndexError):
                    # A metric the template wanted is missing — fall through
                    # to the plain detail rather than raising mid-alert.
                    pass
        return esc(alert.detail)

    def render_alert(self, alert: Alert) -> str:
        icon = _LEVEL_ICON.get(alert.level, "⚠️")
        titles = _TITLES.get(self.lang, _TITLES["en"])
        title = titles.get((alert.kind, alert.level), alert.check)
        body = self._body(alert)
        if alert.ongoing > 0:
            note = _LABELS[self.lang]["ongoing"].format(
                duration=fmt_duration(alert.ongoing, self.lang),
            )
            body = f"{body}\n\n{note}"
        return self._with_footer(f"{icon} <b>{esc(title)}</b>\n\n{body}")

    def render_group(self, alerts: list[Alert]) -> str:
        """Several alerts that fired together, as one message: worst first,
        one line each. The per-alert templates are too long to repeat
        many times over, so a line is the title plus the check's plain detail."""
        labels = _LABELS[self.lang]
        titles = _TITLES.get(self.lang, _TITLES["en"])
        ordered = sorted(
            alerts, key=lambda a: {"crit": 0, "warn": 1, "ok": 2}.get(a.level, 3),
        )
        parts = [f"⚠️ <b>{len(alerts)} {labels['group']}</b>\n"]
        used = len(parts[0])
        shown = 0
        for alert in ordered:
            icon = _LEVEL_ICON.get(alert.level, "⚠️")
            title = titles.get((alert.kind, alert.level), alert.check)
            detail = trunc(alert.detail, _GROUP_LINE)
            line = f"{icon} <b>{esc(title)}</b>"
            if alert.ongoing > 0:
                line += f"  ⏳ {fmt_duration(alert.ongoing, self.lang)}"
            if alert.check and alert.check not in title:
                line += f"  ·  <code>{esc(trunc(alert.check, 60))}</code>"
            if detail:
                line += f"\n{esc(detail)}"
            if shown and used + len(line) > _GROUP_BUDGET:
                break
            parts.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(ordered):
            parts.append(labels["more"].format(n=len(ordered) - shown))
        return self._with_footer("\n".join(parts))

    def render_log_first(self, source: str, sample: str) -> str:
        label = _LABELS[self.lang]["log_first"]
        return self._with_footer(
            f"🔴 <b>{label}</b>  ·  <code>{esc(source)}</code>\n\n"
            f"<pre>{esc(trunc(sample, 600))}</pre>"
        )

    def render_log_digest(self, items: list[dict], period: str = "") -> str:
        label = _LABELS[self.lang]["log_digest"]
        head = f"📜 <b>{label}</b>"
        if period:
            head += f"  ·  <i>{esc(period)}</i>"
        parts = [head]
        for item in items:
            parts.append(
                f"\n📦 <code>{esc(item['source'])}</code>  ·  <b>{item['count']}×</b>\n"
                f"<pre>{esc(trunc(item['sample'], 250))}</pre>"
            )
        return self._with_footer("\n".join(parts))

    # ── buttons ──────────────────────────────────────────────────────────
    # callback_data is `a:<id>` (acknowledge) or `m:<id>:<minutes>` (mute);
    # <id> is a row in the `targets` table, because the alert name itself
    # can be longer than Telegram's 64-byte limit. See bot.py.

    def mute_markup(self, target_id: int, ack: bool = False) -> dict | None:
        if not (self.interactive and target_id):
            return None
        labels = _LABELS[self.lang]
        rows = [[
            {"text": labels["btn_mute1"], "callback_data": f"m:{target_id}:60"},
            {"text": labels["btn_mute8"], "callback_data": f"m:{target_id}:480"},
        ]]
        if ack:
            rows.append([{"text": labels["btn_ack"], "callback_data": f"a:{target_id}"}])
        return {"inline_keyboard": rows}

    def alert_markup(self, alert: Alert) -> dict | None:
        """Buttons for a single bad alert. Recoveries get none (nothing to
        silence), and a grouped message gets none (a button there would
        have to mean "all of them")."""
        if alert.level not in ("warn", "crit"):
            return None
        return self.mute_markup(alert.target_id, ack=alert.level == "crit")

    # ── raw Bot API ──────────────────────────────────────────────────────

    def http_client(self, timeout: float) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": timeout}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def api_call(
        self, method: str, payload: dict, *, timeout: float = 15,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[int, dict]:
        """One Bot API call. Returns (http_status, json) — (0, {}) when the
        network failed. No retries: the polling loop owns its own backoff
        and everything else here is best-effort."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            if client is not None:
                response = await client.post(url, json=payload)
            else:
                async with self.http_client(timeout) as own:
                    response = await own.post(url, json=payload)
        except Exception as e:
            log.warning("telegram %s failed (%s)", method, type(e).__name__)
            return 0, {}
        try:
            body = response.json()
        except Exception:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    # ── sending ──────────────────────────────────────────────────────────

    async def send_alert(self, alert: Alert, silent: bool = False) -> None:
        await self.send_text(
            self.render_alert(alert), silent=silent,
            reply_markup=self.alert_markup(alert),
        )

    async def send_group(self, alerts: list[Alert], silent: bool = False) -> None:
        await self.send_text(self.render_group(alerts), silent=silent)

    def render_startup(self, unclean: str | None = None, last_seen_ago: float = 0.0) -> str:
        """`unclean` is None for a normal start, "reboot" or "crash" when
        the previous run never said goodbye."""
        labels = _LABELS[self.lang]
        text = f"🟢 <b>{labels['startup']}</b>"
        if unclean in ("reboot", "crash"):
            when = labels["ago"].format(
                duration=fmt_duration(last_seen_ago, self.lang),
            )
            text += "\n\n" + labels[unclean].format(when=when)
        return self._with_footer(text)

    async def send_startup(
        self, unclean: str | None = None, last_seen_ago: float = 0.0,
        silent: bool = False,
    ) -> None:
        await self.send_text(
            self.render_startup(unclean, last_seen_ago),
            # An unexpected restart is news; never swallow it.
            silent=silent and unclean is None,
        )

    async def send_shutdown(self, silent: bool = False) -> None:
        label = _LABELS[self.lang]["shutdown"]
        await self.send_text(self._with_footer(f"⏹ <b>{label}</b>"), silent=silent)

    async def send_notice(self, key: str, detail: str = "", silent: bool = False) -> None:
        """A short daemon-level message: `reloaded` or `reload_failed`."""
        label = _LABELS[self.lang][key]
        icon = "♻️" if key == "reloaded" else "⚠️"
        body = f"{icon} <b>{label}</b>"
        if detail:
            body += f"\n\n<pre>{esc(trunc(detail, 800))}</pre>"
        await self.send_text(self._with_footer(body), silent=silent)

    async def send_log_first(
        self, source: str, sample: str, silent: bool = False, target_id: int = 0,
    ) -> None:
        await self.send_text(
            self.render_log_first(source, sample), silent=silent,
            reply_markup=self.mute_markup(target_id),
        )

    async def send_log_digest(
        self, items: list[dict], period: str = "", silent: bool = False,
    ) -> None:
        await self.send_text(self.render_log_digest(items, period), silent=silent)

    async def send_text(
        self, text: str, silent: bool = False, reply_markup: dict | None = None,
    ) -> None:
        """POST one message. Retries on 429 and on transient network errors;
        never raises — a monitoring daemon must not die because Telegram
        had a bad minute. `silent` delivers it without a notification
        sound (Telegram's `disable_notification`)."""
        text = trunc(text, _MAX_LEN)
        kwargs: dict = {"timeout": 15}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if silent:
            payload["disable_notification"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    response = await client.post(self.url, json=payload)
            except Exception as e:
                log.warning("telegram send failed (%s), attempt %d/3",
                            type(e).__name__, attempt + 1)
                await asyncio.sleep(2 ** attempt)
                continue

            if response.status_code == 200:
                return
            if response.status_code == 429:
                # Telegram tells us exactly how long to wait; honour it.
                retry_after = 5
                try:
                    retry_after = int(
                        response.json().get("parameters", {}).get("retry_after", 5)
                    )
                except Exception:
                    pass
                log.warning("telegram rate-limited, retrying in %ds", retry_after)
                await asyncio.sleep(min(retry_after, 30))
                continue
            # 400 = our own bad HTML or a wrong chat_id; retrying won't help.
            log.error("telegram send failed: %s %s",
                      response.status_code, trunc(response.text, 300))
            return
        log.error("telegram send gave up after 3 attempts")
