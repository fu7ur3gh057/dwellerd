"""Locale-aware formatting for outgoing messages. Two languages, en and ru."""
from __future__ import annotations

import time

_MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"],
}


def normalize_lang(lang: str) -> str:
    return "ru" if str(lang).lower().startswith("ru") else "en"


def fmt_now(lang: str) -> str:
    t = time.localtime()
    if normalize_lang(lang) == "ru":
        return f"{t.tm_mday} {_MONTHS['ru'][t.tm_mon - 1]}, {t.tm_hour:02d}:{t.tm_min:02d}"
    return f"{_MONTHS['en'][t.tm_mon - 1]} {t.tm_mday}, {t.tm_hour:02d}:{t.tm_min:02d}"


def fmt_duration(seconds: float, lang: str = "en") -> str:
    """Compact duration: 3d 4h / 4h 20m / 20m / 45s."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if normalize_lang(lang) == "ru":
        d, h, m, s = "д", "ч", "м", "с"
    else:
        d, h, m, s = "d", "h", "m", "s"
    if days:
        return f"{days}{d} {hours}{h}"
    if hours:
        return f"{hours}{h} {minutes}{m}"
    if minutes:
        return f"{minutes}{m}"
    return f"{secs}{s}"


def uptime() -> float:
    """Host uptime in seconds; 0 where /proc/uptime doesn't exist (macOS)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
