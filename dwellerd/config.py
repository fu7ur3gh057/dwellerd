"""YAML config -> dataclasses.

One file is the whole configuration. There is no database of settings, no
runtime editing, no "the DB is the source of truth now" surprise: you edit
the YAML and restart the unit. Everything has a default, so a config with
nothing but a bot token and a chat id is a valid config.

Path priority: $DWELLERD_CONFIG, then /etc/dwellerd/config.yaml, then
./config.yaml next to the checkout (dev mode).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SYSTEM_CONFIG = Path("/etc/dwellerd/config.yaml")
DEV_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def config_path() -> Path:
    """Where the config lives, honouring the env override."""
    env = os.environ.get("DWELLERD_CONFIG")
    if env:
        return Path(env)
    if SYSTEM_CONFIG.exists():
        return SYSTEM_CONFIG
    return DEV_CONFIG


def default_data_dir() -> Path:
    """State dir that matches whichever config we are running from."""
    if config_path() == SYSTEM_CONFIG:
        return Path("/var/lib/dwellerd")
    return DEV_CONFIG.parent / "data"


# ── config sections ──────────────────────────────────────────────────────


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    lang: str = "en"
    proxy: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class ThresholdConfig:
    """A percentage check: on when enabled, warns at `warn`, crits at `crit`."""
    enabled: bool = True
    warn: float = 80.0
    crit: float = 90.0


@dataclass
class DiskConfig:
    path: str = "/"
    warn: float = 80.0
    crit: float = 90.0


@dataclass
class HttpConfig:
    name: str = ""
    url: str = ""
    expect_status: int = 200
    timeout: float = 10.0


@dataclass
class DockerConfig:
    """Container-state watching.

    `containers` empty means "every container docker knows about" — the
    common case on a box where everything in compose is supposed to be up.
    Naming containers explicitly also lets us alert on one that vanished
    entirely, which a bare `docker ps` cannot tell you.
    """
    enabled: bool = False
    containers: list[str] = field(default_factory=list)
    compose: list[str] = field(default_factory=list)


@dataclass
class ChecksConfig:
    interval: float = 60.0
    cpu: ThresholdConfig = field(default_factory=ThresholdConfig)
    memory: ThresholdConfig = field(default_factory=ThresholdConfig)
    swap: ThresholdConfig = field(
        default_factory=lambda: ThresholdConfig(enabled=True, warn=50.0, crit=80.0)
    )
    load: ThresholdConfig = field(
        # Thresholds are load-per-core, so they hold on any core count.
        default_factory=lambda: ThresholdConfig(enabled=True, warn=2.0, crit=4.0)
    )
    disks: list[DiskConfig] = field(default_factory=lambda: [DiskConfig()])
    http: list[HttpConfig] = field(default_factory=list)
    systemd: list[str] = field(default_factory=list)
    docker: DockerConfig = field(default_factory=DockerConfig)


@dataclass
class LogSourceConfig:
    type: str = "file"          # file | journal | docker | docker_container
    name: str = ""
    pattern: str = ".+"
    path: str = ""              # file
    unit: str = ""              # journal
    container: str = ""         # docker_container
    compose: str = ""           # docker (compose service)
    service: str = ""           # docker (compose service)


@dataclass
class LogsConfig:
    enabled: bool = True
    notify: bool = True         # false = capture + digest only, no instant alert
    level: str = "error"        # all | info | warn | error
    digest_interval: float = 3600.0
    retention_days: int = 14
    max_rows: int = 200_000
    sources: list[LogSourceConfig] = field(default_factory=list)


@dataclass
class ReportConfig:
    enabled: bool = True
    interval: float = 10800.0   # 3h
    disks: list[str] = field(default_factory=lambda: ["/"])
    interfaces: list[str] = field(default_factory=list)


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    hostname: str = ""
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    db_path: str = ""
    source_path: Path | None = None

    @property
    def db(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        return default_data_dir() / "dwellerd.sqlite"


# ── parsing ──────────────────────────────────────────────────────────────


def _f(d: dict, key: str, default):
    """Fetch with a default that also survives an explicit YAML `null`."""
    value = d.get(key, default)
    return default if value is None else value


def _threshold(raw, fallback: ThresholdConfig) -> ThresholdConfig:
    if raw is None:
        return fallback
    if isinstance(raw, bool):          # `cpu: false` disables it outright
        return ThresholdConfig(enabled=raw, warn=fallback.warn, crit=fallback.crit)
    if not isinstance(raw, dict):
        return fallback
    return ThresholdConfig(
        enabled=bool(_f(raw, "enabled", True)),
        warn=float(_f(raw, "warn", fallback.warn)),
        crit=float(_f(raw, "crit", fallback.crit)),
    )


def _disks(raw) -> list[DiskConfig]:
    if not raw:
        return [DiskConfig()]
    out = []
    for item in raw:
        if isinstance(item, str):      # bare path shorthand: `- /var`
            out.append(DiskConfig(path=item))
        elif isinstance(item, dict):
            out.append(DiskConfig(
                path=str(_f(item, "path", "/")),
                warn=float(_f(item, "warn", 80.0)),
                crit=float(_f(item, "crit", 90.0)),
            ))
    return out or [DiskConfig()]


def _https(raw) -> list[HttpConfig]:
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(HttpConfig(name=item, url=item))
        elif isinstance(item, dict):
            url = str(_f(item, "url", ""))
            if not url:
                continue
            out.append(HttpConfig(
                name=str(_f(item, "name", "")) or url,
                url=url,
                expect_status=int(_f(item, "expect_status", 200)),
                timeout=float(_f(item, "timeout", 10.0)),
            ))
    return out


def _log_sources(raw) -> list[LogSourceConfig]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        src = LogSourceConfig(
            type=str(_f(item, "type", "file")),
            name=str(_f(item, "name", "")),
            pattern=str(_f(item, "pattern", ".+")),
            path=str(_f(item, "path", "")),
            unit=str(_f(item, "unit", "")),
            container=str(_f(item, "container", "")),
            compose=str(_f(item, "compose", "")),
            service=str(_f(item, "service", "")),
        )
        if not src.name:
            # Name is the dedup scope and the label in Telegram — derive a
            # usable one rather than dropping the source.
            src.name = src.container or src.unit or src.service or src.path or src.type
        out.append(src)
    return out


def parse(raw: dict, source: Path | None = None) -> Config:
    raw = raw or {}
    cfg = Config(source_path=source)

    tg = _f(raw, "telegram", {}) or {}
    cfg.telegram = TelegramConfig(
        bot_token=str(_f(tg, "bot_token", "")),
        chat_id=str(_f(tg, "chat_id", "")),
        lang=str(_f(tg, "lang", "en")),
        proxy=str(_f(tg, "proxy", "")),
    )

    cfg.hostname = str(_f(raw, "hostname", ""))
    cfg.db_path = str(_f(raw, "db_path", ""))

    ch = _f(raw, "checks", {}) or {}
    defaults = ChecksConfig()
    dk = _f(ch, "docker", {}) or {}
    cfg.checks = ChecksConfig(
        interval=float(_f(ch, "interval", 60.0)),
        cpu=_threshold(ch.get("cpu"), defaults.cpu),
        memory=_threshold(ch.get("memory"), defaults.memory),
        swap=_threshold(ch.get("swap"), defaults.swap),
        load=_threshold(ch.get("load"), defaults.load),
        disks=_disks(ch.get("disks")),
        http=_https(ch.get("http")),
        systemd=[str(u) for u in (_f(ch, "systemd", []) or [])],
        docker=DockerConfig(
            enabled=bool(_f(dk, "enabled", False)),
            containers=[str(c) for c in (_f(dk, "containers", []) or [])],
            compose=[str(c) for c in (_f(dk, "compose", []) or [])],
        ),
    )

    lg = _f(raw, "logs", {}) or {}
    cfg.logs = LogsConfig(
        enabled=bool(_f(lg, "enabled", True)),
        notify=bool(_f(lg, "notify", True)),
        level=str(_f(lg, "level", "error")),
        digest_interval=float(_f(lg, "digest_interval", 3600.0)),
        retention_days=int(_f(lg, "retention_days", 14)),
        max_rows=int(_f(lg, "max_rows", 200_000)),
        sources=_log_sources(lg.get("sources")),
    )

    rp = _f(raw, "report", {}) or {}
    cfg.report = ReportConfig(
        enabled=bool(_f(rp, "enabled", True)),
        interval=float(_f(rp, "interval", 10800.0)),
        disks=[str(d) for d in (_f(rp, "disks", []) or [])]
              or [d.path for d in cfg.checks.disks],
        interfaces=[str(i) for i in (_f(rp, "interfaces", []) or [])],
    )
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path} — run `dwellerd setup` (or `make setup`) first"
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return parse(raw, source=path)
