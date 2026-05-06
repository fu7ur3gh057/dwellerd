from config import CheckConfig
from .base import Check, Result
from .cpu import CpuCheck
from .disk import DiskCheck
from .http import HttpCheck
from .memory import MemoryCheck
from .systemd_unit import SystemdUnitCheck

__all__ = ["Check", "Result", "build_check"]


def build_check(cfg: CheckConfig) -> Check:
    if cfg.type == "http":
        return HttpCheck(name=cfg.name, interval=cfg.interval, **cfg.options)
    if cfg.type == "cpu":
        return CpuCheck(name=cfg.name, interval=cfg.interval, **cfg.options)
    if cfg.type == "memory":
        return MemoryCheck(name=cfg.name, interval=cfg.interval, **cfg.options)
    if cfg.type == "disk":
        return DiskCheck(name=cfg.name, interval=cfg.interval, **cfg.options)
    if cfg.type == "systemd":
        return SystemdUnitCheck(name=cfg.name, interval=cfg.interval, **cfg.options)
    raise ValueError(f"unknown check type: {cfg.type}")
