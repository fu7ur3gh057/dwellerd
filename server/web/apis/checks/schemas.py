from pydantic import BaseModel


class CheckSummary(BaseModel):
    name: str
    type: str
    interval: float
    level: str | None = None  # last known severity, None if never run
    last_run_ts: float | None = None
    last_value: float | None = None  # for cpu/mem/disk: percent; None otherwise
    last_detail: str | None = None


class RunResponse(BaseModel):
    name: str
    queued: bool
