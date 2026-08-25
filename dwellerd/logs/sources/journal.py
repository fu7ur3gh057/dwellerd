"""systemd-journal source — follows `journalctl -u <unit>`.

`-n 0` means no history: only entries that arrive after the daemon starts,
matching the file source's tail-from-end behaviour. `-o cat` strips journal
metadata so the matched line is just the message. Requires the dwellerd
user to be in the `systemd-journal` group (the installer handles that).
"""
from __future__ import annotations

from .base import SubprocessLogSource


class JournalLogSource(SubprocessLogSource):
    type = "journal"

    def __init__(
        self, name: str, unit: str, pattern: str = ".+", reconnect_delay: float = 5.0,
    ) -> None:
        super().__init__(name=name, pattern=pattern, reconnect_delay=reconnect_delay)
        self.unit = unit

    def _argv(self) -> list[str]:
        return ["journalctl", "-u", self.unit, "-f", "-n", "0", "-o", "cat", "--no-pager"]
