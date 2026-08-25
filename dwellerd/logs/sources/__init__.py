"""Log source registry — config entries in, live source objects out."""
from __future__ import annotations

import logging

from ...config import LogSourceConfig
from .base import LogSource, SubprocessLogSource
from .docker import DockerComposeLogSource, DockerContainerLogSource
from .file import FileLogSource
from .journal import JournalLogSource

log = logging.getLogger(__name__)

__all__ = [
    "LogSource", "SubprocessLogSource",
    "FileLogSource", "JournalLogSource",
    "DockerContainerLogSource", "DockerComposeLogSource",
    "build_source", "build_sources",
]


def build_source(cfg: LogSourceConfig) -> LogSource | None:
    """Build one source, or None if the config entry is unusable. A bad
    entry is logged and skipped — one typo shouldn't stop the daemon."""
    try:
        if cfg.type == "file":
            if not cfg.path:
                raise ValueError("`path` is required for a file source")
            return FileLogSource(cfg.name, cfg.path, cfg.pattern)
        if cfg.type == "journal":
            if not cfg.unit:
                raise ValueError("`unit` is required for a journal source")
            return JournalLogSource(cfg.name, cfg.unit, cfg.pattern)
        if cfg.type == "docker_container":
            if not cfg.container:
                raise ValueError("`container` is required for a docker_container source")
            return DockerContainerLogSource(cfg.name, cfg.container, cfg.pattern)
        if cfg.type == "docker":
            if not (cfg.compose and cfg.service):
                raise ValueError("`compose` and `service` are required for a docker source")
            return DockerComposeLogSource(cfg.name, cfg.compose, cfg.service, cfg.pattern)
        raise ValueError(f"unknown log source type {cfg.type!r}")
    except Exception as e:
        log.error("skipping log source %r: %s", cfg.name or cfg.type, e)
        return None


def build_sources(configs: list[LogSourceConfig]) -> list[LogSource]:
    sources = []
    seen: set[str] = set()
    for cfg in configs:
        source = build_source(cfg)
        if source is None:
            continue
        if source.name in seen:
            # Names scope the dedup signatures; duplicates would silently
            # merge two sources' error histories.
            log.error("duplicate log source name %r — skipping the second one",
                      source.name)
            continue
        seen.add(source.name)
        sources.append(source)
    return sources
