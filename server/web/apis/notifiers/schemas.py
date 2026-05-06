from pydantic import BaseModel


class NotifierInfo(BaseModel):
    type: str
    lang: str | None = None


class TestRequest(BaseModel):
    message: str = "Test alert from Dwellerd"
    level: str = "warn"
