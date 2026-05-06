"""Text formatting helpers for bot responses.

Telegram supports HTML formatting; we set parse_mode=HTML on the Bot
instance, so handlers return strings with <b>, <i>, <code>, etc.

Keep functions small and pure — they're called from many places.
"""
from __future__ import annotations

import html
import time


def size_h(num_bytes: int | float) -> str:
    """Human-readable byte size (B / KB / MB / GB / TB)."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pct_bar(pct: float, width: int = 10) -> str:
    """ASCII progress bar — `█` filled + `░` empty."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct * width / 100))
    return "█" * filled + "░" * (width - filled)


def fmt_pct(pct: float) -> str:
    """Coloured percentage hint via emoji."""
    if pct >= 90:
        return f"🔴 {pct:.0f}%"
    if pct >= 75:
        return f"🟡 {pct:.0f}%"
    return f"🟢 {pct:.0f}%"


def level_emoji(level: str) -> str:
    """Map check/alert level to a status dot."""
    return {
        "ok": "🟢",
        "warn": "🟡",
        "crit": "🔴",
    }.get(level, "⚪")


def time_ago(ts: float, *, now: float | None = None) -> str:
    """`12s`, `5m`, `3h`, `2d`. Empty string for ts == 0."""
    if not ts:
        return "—"
    delta = max(0.0, (now or time.time()) - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def short_dt(ts: float) -> str:
    """`HH:MM` for today, `dd.MM HH:MM` otherwise."""
    if not ts:
        return "—"
    now = time.localtime()
    t = time.localtime(ts)
    if (t.tm_year, t.tm_mon, t.tm_mday) == (now.tm_year, now.tm_mon, now.tm_mday):
        return time.strftime("%H:%M", t)
    return time.strftime("%d.%m %H:%M", t)


def esc(s: object) -> str:
    """HTML-escape — anything that might hit Telegram's HTML parser."""
    return html.escape(str(s), quote=False)


def code(s: object) -> str:
    """Wrap in <code> with HTML escape."""
    return f"<code>{esc(s)}</code>"
