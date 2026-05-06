"""/report — full system digest (same as the periodic Telegram digest)."""
from __future__ import annotations

import asyncio
import logging
import socket

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..queries import get_settings


router = Router(name=__name__)
log = logging.getLogger(__name__)


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    """Re-uses the daemon's report builder so output matches the periodic
    Telegram digest exactly. Heavy-ish — runs psutil + docker probes —
    but acceptable as an on-demand command (rate-limited by the user
    pressing /report manually)."""
    settings = get_settings()
    cfg = (settings.report if settings else None) or {}
    if not cfg:
        await message.answer(
            "В конфиге нет блока <code>report</code> — настрой через "
            "<code>make setup</code> или web UI."
        )
        return

    hostname = cfg.get("hostname") or socket.gethostname()
    lang = (cfg.get("lang") or "ru")

    # build_report_context returns {hostname, lang, sections, targets} or None.
    try:
        from core.report import build_report_context  # type: ignore
        from core.report.builder import assemble  # type: ignore
    except ImportError:
        await message.answer("server/core не на PYTHONPATH — запусти через make run-bot.")
        return

    rep = build_report_context(cfg, lang=lang, hostname=hostname)
    if rep is None or not rep.get("sections"):
        await message.answer("Нечего рендерить — секции отчёта не настроены.")
        return

    notice = await message.answer("⏳ Собираю отчёт…")

    async def _render(s):
        try:
            return await s.render()
        except Exception:
            log.exception("report section %s failed", type(s).__name__)
            return None

    results = await asyncio.gather(*(_render(s) for s in rep["sections"]))
    body = assemble(rep["hostname"], [r for r in results if r is not None], lang=lang)

    try:
        await notice.delete()
    except Exception:
        pass

    # Telegram message size cap is 4096 chars. The report is normally well
    # under that; trim defensively if not.
    if len(body) > 4000:
        body = body[:3990] + "\n…⟨обрезано⟩"
    await message.answer(body)
