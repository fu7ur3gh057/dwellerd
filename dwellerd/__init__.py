"""Dwellerd — a small monitoring daemon for a single Linux host.

One process, one systemd unit, one YAML file, one Telegram chat. It runs
periodic checks (cpu / memory / disk / http / systemd units / docker
containers), tails logs with dedup, and pushes alerts + a periodic report
to Telegram. No web UI, no bot commands, no task queue, no ORM.
"""

__version__ = "1.0.0"
