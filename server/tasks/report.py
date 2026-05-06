"""Periodic system-status digest, scheduled via the broker."""

import asyncio
import logging

from core.report.builder import assemble
from core.report.sections.base import SectionResult
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from services.taskiq.deps import get_app_context
from taskiq import TaskiqDepends

log = logging.getLogger(__name__)


@broker.task
async def build_and_send_report(
    ctx: AppContext = TaskiqDepends(get_app_context),
) -> None:
    if not ctx.report_sections or not ctx.report_targets:
        return

    results = await asyncio.gather(
        *(_render(s) for s in ctx.report_sections), return_exceptions=False,
    )
    message = assemble(ctx.report_hostname, list(results), lang=ctx.report_lang)
    for n in ctx.report_targets:
        try:
            await n.send_text(message)
        except Exception:
            log.exception("notifier %s failed for report", type(n).__name__)


async def _render(section) -> SectionResult:
    try:
        return await section.render()
    except Exception as e:
        log.exception("section %s crashed", type(section).__name__)
        name = type(section).__name__
        return SectionResult(text=f"⚠️ {name}: {e}", warnings=[f"{name}: {e}"])
