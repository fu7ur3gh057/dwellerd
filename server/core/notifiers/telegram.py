import logging

import httpx

from core.i18n import fmt_now
from .base import Alert

log = logging.getLogger(__name__)

_LEVEL_ICON = {"ok": "✅", "warn": "🟡", "crit": "🔴"}

_LABELS = {
    "en": {
        "startup": "Monitoring connected",
        "shutdown": "Monitoring stopped",
        "log_first": "New error",
        "log_digest": "Error digest",
        "log_digest_period": "last period",
    },
    "ru": {
        "startup": "Мониторинг подключен",
        "shutdown": "Мониторинг остановлен",
        "log_first": "Новая ошибка",
        "log_digest": "Дайджест ошибок",
        "log_digest_period": "за последний период",
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
        ("disk", "warn"): "Disk space running low",
        ("disk", "crit"): "Disk space critical",
        ("disk", "ok"): "Disk space recovered",
        ("http", "warn"): "HTTP check warning",
        ("http", "crit"): "HTTP endpoint down",
        ("http", "ok"): "HTTP endpoint recovered",
        ("systemd", "warn"): "Service degraded",
        ("systemd", "crit"): "Service is down",
        ("systemd", "ok"): "Service is back up",
    },
    "ru": {
        ("cpu", "warn"): "Повышенная нагрузка на CPU",
        ("cpu", "crit"): "Критическая нагрузка на CPU",
        ("cpu", "ok"): "CPU вернулся в норму",
        ("memory", "warn"): "Повышенный расход памяти",
        ("memory", "crit"): "Критическая загрузка памяти",
        ("memory", "ok"): "Память вернулась в норму",
        ("disk", "warn"): "Места на диске мало",
        ("disk", "crit"): "Критически мало места на диске",
        ("disk", "ok"): "Место на диске восстановлено",
        ("http", "warn"): "Эндпоинт отвечает с предупреждениями",
        ("http", "crit"): "Эндпоинт недоступен",
        ("http", "ok"): "Эндпоинт снова доступен",
        ("systemd", "warn"): "Сервис деградировал",
        ("systemd", "crit"): "Сервис не активен",
        ("systemd", "ok"): "Сервис снова работает",
    },
}

# Percentage values are wrapped in <tg-spoiler> so they aren't visible at a
# glance in chat previews / over-the-shoulder views — tap to reveal.
_BODIES_FIRING = {
    "en": {
        "cpu": "CPU is at <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
               "(threshold: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>).",
        "memory": "Memory is at <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
                  "(threshold: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>).",
        "disk": "Partition <code>{path}</code> is at <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
                "(threshold: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>, "
                "free: <b>{free_gb:.1f} GB</b>).",
        "http": "<code>{url}</code> — <b>{summary}</b>",
        "systemd": "Unit <code>{unit}</code> state: <b>{state}</b>",
    },
    "ru": {
        "cpu": "CPU загружен на <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
               "(порог: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>).",
        "memory": "Память занята на <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
                  "(порог: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>).",
        "disk": "Раздел <code>{path}</code> заполнен на <tg-spoiler><b>{value:.1f}%</b></tg-spoiler> "
                "(порог: <tg-spoiler><b>{threshold:.0f}%</b></tg-spoiler>, "
                "свободно: <b>{free_gb:.1f} ГБ</b>).",
        "http": "<code>{url}</code> — <b>{summary}</b>",
        "systemd": "Юнит <code>{unit}</code> в состоянии: <b>{state}</b>",
    },
}

_BODIES_OK = {
    "en": {
        "cpu": "CPU is now at <tg-spoiler><b>{value:.1f}%</b></tg-spoiler>.",
        "memory": "Memory is now at <tg-spoiler><b>{value:.1f}%</b></tg-spoiler>.",
        "disk": "Partition <code>{path}</code> is back to "
                "<tg-spoiler><b>{value:.1f}%</b></tg-spoiler> used.",
        "http": "<code>{url}</code> is responding again.",
        "systemd": "Unit <code>{unit}</code> is active again.",
    },
    "ru": {
        "cpu": "CPU вернулся к <tg-spoiler><b>{value:.1f}%</b></tg-spoiler>.",
        "memory": "Память вернулась к <tg-spoiler><b>{value:.1f}%</b></tg-spoiler>.",
        "disk": "Раздел <code>{path}</code> вернулся к "
                "<tg-spoiler><b>{value:.1f}%</b></tg-spoiler>.",
        "http": "<code>{url}</code> снова отвечает.",
        "systemd": "Юнит <code>{unit}</code> снова активен.",
    },
}


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trunc(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        lang: str = "en",
        proxy: str | None = None,
    ) -> None:
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.lang = lang if lang in _TITLES else "en"
        self.proxy = proxy or None

    async def send(self, alert: Alert) -> None:
        icon = _LEVEL_ICON.get(alert.level, "⚠️")
        title = _TITLES.get(self.lang, _TITLES["en"]).get(
            (alert.kind, alert.level), alert.check,
        )
        body = self._render_body(alert)
        text = (
            f"{icon} <b>{_esc(title)}</b>\n\n"
            f"{body}\n\n"
            f"<i>{fmt_now(self.lang)}</i>"
        )
        await self._send(text)

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def send_startup(self) -> None:
        label = _LABELS[self.lang]["startup"]
        text = (
            f"🟢 <b>{label}</b>\n\n"
            f"<i>{fmt_now(self.lang)}</i>"
        )
        await self._send(text)

    async def send_shutdown(self) -> None:
        label = _LABELS[self.lang]["shutdown"]
        text = (
            f"⏹ <b>{label}</b>\n\n"
            f"<i>{fmt_now(self.lang)}</i>"
        )
        await self._send(text)

    async def send_log_first(self, source: str, sample: str) -> None:
        label = _LABELS[self.lang]["log_first"]
        text = (
            f"📜 <b>{label}</b>  ·  <code>{_esc(source)}</code>\n\n"
            f"<pre>{_esc(_trunc(sample, 600))}</pre>\n"
            f"<i>{fmt_now(self.lang)}</i>"
        )
        await self._send(text)

    async def send_log_digest(self, items: list[dict], period_label: str = "") -> None:
        label = _LABELS[self.lang]["log_digest"]
        period = period_label or _LABELS[self.lang]["log_digest_period"]
        parts = [f"📜 <b>{label}</b>  ·  <i>{_esc(period)}</i>"]
        for item in items:
            parts.append(
                f"\n📦 <code>{_esc(item['source'])}</code>  ·  <b>{item['count']}×</b>\n"
                f"<pre>{_esc(_trunc(item['sample'], 250))}</pre>"
            )
        parts.append(f"\n<i>{fmt_now(self.lang)}</i>")
        await self._send("\n".join(parts))

    def _render_body(self, alert: Alert) -> str:
        if alert.kind and alert.metrics:
            tmpls = _BODIES_OK if alert.level == "ok" else _BODIES_FIRING
            tmpl = tmpls.get(self.lang, tmpls["en"]).get(alert.kind)
            if tmpl:
                try:
                    return tmpl.format(**alert.metrics)
                except (KeyError, ValueError):
                    pass
        return _esc(alert.detail)

    async def _send(self, text: str) -> None:
        kwargs: dict = {"timeout": 10}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.post(
                self.url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        if response.status_code != 200:
            log.error("telegram send failed: %s %s", response.status_code, response.text)
