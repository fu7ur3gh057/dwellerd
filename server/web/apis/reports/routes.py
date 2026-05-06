"""Reports — preview the digest on demand without sending it to Telegram."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from core.report.builder import assemble
from core.report.sections.base import SectionResult
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from web.apis.reports.schemas import ReportPreview

log = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])


@router.post("/preview", response_model=ReportPreview)
async def preview(
    ctx: AppContext = Depends(lambda: broker.state.app_ctx),
) -> ReportPreview:
    if not ctx.report_sections:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="no report sections configured (config.report)",
        )

    results = await asyncio.gather(
        *(_render_safe(s) for s in ctx.report_sections),
    )
    warnings: list[str] = []
    for r in results:
        warnings.extend(r.warnings)

    html = assemble(ctx.report_hostname, list(results), lang=ctx.report_lang)
    return ReportPreview(
        hostname=ctx.report_hostname,
        lang=ctx.report_lang,
        html=html,
        warnings=warnings,
    )


async def _render_safe(section) -> SectionResult:
    try:
        return await section.render()
    except Exception as e:
        name = type(section).__name__
        log.exception("preview: section %s crashed", name)
        return SectionResult(text=f"⚠️ {name}: {e}", warnings=[f"{name}: {e}"])
