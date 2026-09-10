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

from ..i18n import normalize_lang
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
    },
    "ru": {
        "startup": "Мониторинг запущен",
        "shutdown": "Мониторинг остановлен",
        "log_first": "Новая ошибка",
        "log_digest": "Дайджест ошибок",
        "test": "Тестовый алерт",
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


def trunc(s, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


class TelegramNotifier:
    def __init__(
        self, bot_token: str, chat_id: str, lang: str = "en",
        proxy: str = "", hostname: str = "",
    ) -> None:
        self.token = bot_token
        self.chat_id = chat_id
        self.lang = normalize_lang(lang)
        self.proxy = proxy or None
        self.hostname = hostname
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
        return self._with_footer(
            f"{icon} <b>{esc(title)}</b>\n\n"
            f"{self._body(alert)}"
        )

    def render_log_first(self, source: str, sample: str) -> str:
        label = _LABELS[self.lang]["log_first"]
        return self._with_footer(
            f"🚨 <b>{label}</b>  ·  <code>{esc(source)}</code>\n\n"
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

    # ── sending ──────────────────────────────────────────────────────────

    async def send_alert(self, alert: Alert) -> None:
        await self.send_text(self.render_alert(alert))

    async def send_startup(self) -> None:
        label = _LABELS[self.lang]["startup"]
        await self.send_text(self._with_footer(f"🟢 <b>{label}</b>"))

    async def send_shutdown(self) -> None:
        label = _LABELS[self.lang]["shutdown"]
        await self.send_text(self._with_footer(f"⏹ <b>{label}</b>"))

    async def send_log_first(self, source: str, sample: str) -> None:
        await self.send_text(self.render_log_first(source, sample))

    async def send_log_digest(self, items: list[dict], period: str = "") -> None:
        await self.send_text(self.render_log_digest(items, period))

    async def send_text(self, text: str) -> None:
        """POST one message. Retries on 429 and on transient network errors;
        never raises — a monitoring daemon must not die because Telegram
        had a bad minute."""
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
