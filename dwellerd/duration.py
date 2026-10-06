"""Parse human durations for the CLI: `90s`, `30m`, `2h`, `1d`, `1h30m`."""
from __future__ import annotations

import re

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PART = re.compile(r"(\d+)([smhd])")


def parse_duration(text: str) -> float:
    """Seconds for a duration string. A bare number is rejected on purpose:
    `mute 30` could mean seconds, minutes or hours, and guessing wrong
    silences an alert for the wrong length of time."""
    raw = str(text).strip().lower()
    if not raw:
        raise ValueError("empty duration")
    pos, total = 0, 0
    for m in _PART.finditer(raw):
        if m.start() != pos:
            break
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    else:
        if pos == len(raw) and total > 0:
            return float(total)
    raise ValueError(
        f"cannot read {text!r} as a duration — use e.g. 30m, 2h, 1d or 1h30m"
    )
