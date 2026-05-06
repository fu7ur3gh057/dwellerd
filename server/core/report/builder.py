import time

from .sections.base import SectionResult


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_REPORT_LABEL = {"en": "System report", "ru": "Системный отчёт"}
_ALERTS_LABEL = {"en": "alerts", "ru": "алерты"}
_RECS_HEADER = {"en": "💡 Recommendations", "ru": "💡 Рекомендации"}
_DIVIDER = "━" * 22

_RULES: dict[str, list[tuple[str, str]]] = {
    "en": [
        ("not running", "Restart missing containers or check exit reasons"),
        ("unhealthy",   "Investigate unhealthy containers (docker logs)"),
        ("swap",        "Investigate processes using swap; consider adding RAM"),
        ("disk",        "Free disk space — rotate logs, run docker prune"),
        ("ram",         "Check top processes by memory usage"),
        ("cpu",         "Check top processes by CPU usage"),
        ("postgres",    "Check Postgres connections / queries"),
    ],
    "ru": [
        ("not running", "Перезапустить упавшие контейнеры или посмотреть причину"),
        ("unhealthy",   "Проверить unhealthy-контейнеры (docker logs)"),
        ("swap",        "Высокий swap — проверить процессы по памяти / добавить RAM"),
        ("disk",        "Освободить место (логи, docker system prune)"),
        ("ram",         "Проверить топ процессов по памяти"),
        ("cpu",         "Проверить топ процессов по CPU"),
        ("postgres",    "Проверить соединения / запросы Postgres"),
    ],
}


def assemble(hostname: str, sections: list[SectionResult], lang: str = "en") -> str:
    parts = [_header(hostname, lang)]
    warnings: list[str] = []
    for s in sections:
        parts.append(_html_section(s.text))
        warnings.extend(s.warnings)
    if warnings:
        parts.append(_alerts_block(warnings, lang))
    recs = _recommendations(warnings, lang)
    if recs:
        parts.append(recs)
    return "\n\n".join(parts)


def _header(hostname: str, lang: str) -> str:
    label = _REPORT_LABEL.get(lang, _REPORT_LABEL["en"])
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line1 = f"🖥 <b>{label} | {ts}</b>"
    if hostname:
        return f"{line1}\n<i>{_esc(hostname)}</i>\n{_DIVIDER}"
    return f"{line1}\n{_DIVIDER}"


def _html_section(text: str) -> str:
    if "\n" in text:
        first, rest = text.split("\n", 1)
        return f"<b>{_esc(first)}</b>\n{_esc(rest)}"
    return f"<b>{_esc(text)}</b>"


def _alerts_block(warnings: list[str], lang: str) -> str:
    label = _ALERTS_LABEL.get(lang, _ALERTS_LABEL["en"])
    lines = [f"⚠️ <b>{len(warnings)} {label}</b>"]
    for w in warnings:
        lines.append(f"🟡 {_esc(w)}")
    return "\n".join(lines)


def _recommendations(warnings: list[str], lang: str) -> str | None:
    if not warnings:
        return None
    blob = " ".join(warnings).lower()
    rules = _RULES.get(lang, _RULES["en"])
    seen: set[str] = set()
    recs: list[str] = []
    for trigger, advice in rules:
        if trigger in blob and advice not in seen:
            seen.add(advice)
            recs.append(advice)
    if not recs:
        return None
    header = _RECS_HEADER.get(lang, _RECS_HEADER["en"])
    body = "\n".join(f"• {_esc(r)}" for r in recs)
    # body wrapped in spoiler — keeps the report tidy at a glance, tap to reveal.
    return f"<b>{header}</b>\n<tg-spoiler>{body}</tg-spoiler>"
