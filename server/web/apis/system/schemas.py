from pydantic import BaseModel


class DiskUsage(BaseModel):
    path: str
    total_gb: float
    used_gb: float
    free_gb: float
    pct: float


class SystemSnapshot(BaseModel):
    cpu_pct: float
    memory_pct: float
    memory_used_gb: float
    memory_total_gb: float
    swap_pct: float
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_seconds: int
    disks: list[DiskUsage]
