"""Locale-aware date and uptime formatting for outgoing notifications."""
import time

_MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"],
}


def fmt_now(lang: str) -> str:
    t = time.localtime()
    if lang == "ru":
        return f"{t.tm_mday} {_MONTHS['ru'][t.tm_mon - 1]}, {t.tm_hour:02d}:{t.tm_min:02d}"
    return f"{_MONTHS['en'][t.tm_mon - 1]} {t.tm_mday}, {t.tm_hour:02d}:{t.tm_min:02d}"


def fmt_uptime(lang: str) -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
    except OSError:
        return ""
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if lang == "ru":
        d, h, m = "д", "ч", "м"
    else:
        d, h, m = "d", "h", "m"
    if days:
        return f"{days}{d} {hours}{h}"
    if hours:
        return f"{hours}{h} {minutes}{m}"
    return f"{minutes}{m}"


def uptime_label(lang: str) -> str:
    return "аптайм" if lang == "ru" else "up"
