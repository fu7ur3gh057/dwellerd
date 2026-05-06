from pydantic import BaseModel


class ReportPreview(BaseModel):
    hostname: str
    lang: str
    html: str  # rendered Telegram-flavoured HTML; the frontend can either show
               # as-is or strip tags for a plain rendering.
    warnings: list[str]
