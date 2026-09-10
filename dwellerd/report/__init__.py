"""Periodic report — a scheduled "everything is fine" (or not) digest.

Alerts tell you when something changes. The report tells you the box is
still alive and what it looks like right now, which is the message you
actually want at 3am when nothing has fired for six hours.
"""
from __future__ import annotations

import logging
import time

from ..i18n import normalize_lang
from .sections import (
    ChecksSection, DockerSection, HostSection, LogsSection, SectionResult,
)

log = logging.getLogger(__name__)

__all__ = [
    "ReportBuilder", "SectionResult",
    "HostSection", "DockerSection", "LogsSection", "ChecksSection",
]

_TITLE = {"en": "System report", "ru": "Системный отчёт"}
_ALERTS = {"en": "warnings", "ru": "предупреждения"}
_RECS = {"en": "💡 Recommendations", "ru": "💡 Рекомендации"}
_DIVIDER = "━" * 22

# Substring -> advice. Crude on purpose: the warnings are generated a few
# lines above by our own sections, so the vocabulary is known.
_RULES: dict[str, list[tuple[str, str]]] = {
    "en": [
        ("not running", "Restart the missing containers, or check why they exited"),
        ("unhealthy", "Inspect unhealthy containers: docker logs <name>"),
        ("swap", "Something is swapping — check the top processes by memory, or add RAM"),
        ("disk", "Free disk space: rotate logs, docker system prune"),
        ("ram", "Check the top processes by memory usage"),
        ("cpu", "Check the top processes by CPU usage"),
        ("load", "Load is above the core count — look for a runaway process or IO wait"),
        ("crit", "Something is in a critical state — see the checks section above"),
    ],
    "ru": [
        ("not running", "Перезапустить упавшие контейнеры или посмотреть причину выхода"),
        ("unhealthy", "Проверить unhealthy-контейнеры: docker logs <name>"),
        ("swap", "Идёт свопинг — проверить топ процессов по памяти или добавить RAM"),
        ("disk", "Освободить место: ротация логов, docker system prune"),
        ("ram", "Проверить топ процессов по памяти"),
        ("cpu", "Проверить топ процессов по CPU"),
        ("load", "Load выше числа ядер — искать зависший процесс или IO wait"),
        ("crit", "Что-то в критическом состоянии — смотри раздел проверок выше"),
    ],
}


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ReportBuilder:
    def __init__(self, cfg, storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self.lang = normalize_lang(cfg.telegram.lang)
        self.hostname = cfg.hostname
        # Sections that hold state between runs (the network counter) must
        # be built once, not per report.
        self.host = HostSection(
            lang=self.lang,
            disks=cfg.report.disks,
            interfaces=cfg.report.interfaces,
            warn_pct=cfg.checks.memory.warn,
        )
        self.docker = (
            DockerSection(lang=self.lang, containers=cfg.checks.docker.containers)
            if cfg.checks.docker.enabled else None
        )
        self._last_run = time.time()

    async def build(self) -> str:
        since = self._last_run
        self._last_run = time.time()

        sections = [self.host]
        if self.docker is not None:
            sections.append(self.docker)
        sections.append(ChecksSection(self.storage, self.lang))
        if self.cfg.logs.enabled:
            sections.append(LogsSection(self.storage, since, self.lang))

        rendered: list[SectionResult] = []
        for section in sections:
            try:
                rendered.append(await section.render())
            except Exception:
                # One broken section must not cost us the whole report.
                log.exception("report section %s failed", type(section).__name__)

        return self._assemble(rendered)

    def _assemble(self, sections: list[SectionResult]) -> str:
        parts = [self._header()]
        warnings: list[str] = []
        for section in sections:
            parts.append(self._as_html(section.text))
            warnings.extend(section.warnings)
        if warnings:
            parts.append(self._warnings_block(warnings))
            advice = self._recommendations(warnings)
            if advice:
                parts.append(advice)
        return "\n\n".join(parts)

    def _header(self) -> str:
        title = _TITLE.get(self.lang, _TITLE["en"])
        line = f"🖥 <b>{title}</b>"
        if self.hostname:
            line += f"\n<i>{esc(self.hostname)}</i>"
        return f"{line}\n{_DIVIDER}"

    @staticmethod
    def _as_html(text: str) -> str:
        """First line of a section is its bold heading; the body is escaped
        plain text (it contains user data — container names, log samples)."""
        if "\n" in text:
            first, rest = text.split("\n", 1)
            return f"<b>{esc(first)}</b>\n{esc(rest)}"
        return f"<b>{esc(text)}</b>"

    def _warnings_block(self, warnings: list[str]) -> str:
        label = _ALERTS.get(self.lang, _ALERTS["en"])
        lines = [f"⚠️ <b>{len(warnings)} {label}</b>"]
        seen: set[str] = set()
        for warning in warnings:
            if warning in seen:
                continue
            seen.add(warning)
            lines.append(f"🟡 {esc(warning)}")
        return "\n".join(lines)

    def _recommendations(self, warnings: list[str]) -> str | None:
        blob = " ".join(warnings).lower()
        rules = _RULES.get(self.lang, _RULES["en"])
        advice: list[str] = []
        for trigger, text in rules:
            if trigger in blob and text not in advice:
                advice.append(text)
        if not advice:
            return None
        header = _RECS.get(self.lang, _RECS["en"])
        body = "\n".join(f"• {esc(a)}" for a in advice)
        return f"<b>{header}</b>\n{body}"
