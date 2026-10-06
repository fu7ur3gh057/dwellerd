"""Config validation — what `dwellerd config --validate` prints and what a
SIGHUP reload checks before it dares to replace a working configuration.

It works on the raw YAML dict, not on the parsed `Config`: parsing quietly
coerces and defaults, which is exactly what hides a typo. Two severities:

  error    the value cannot work (bad regex, warn >= crit, unknown source
           type). A reload with errors is refused.
  warning  it will run, but probably not the way you meant (unknown key,
           which is nearly always a typo; unusual value).

`validate` never raises; whatever it cannot make sense of is reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import parse_clock

_TOP = {"telegram", "hostname", "checks", "alerts", "logs", "report", "db_path"}
_TELEGRAM = {"bot_token", "chat_id", "lang", "proxy", "interactive", "admins"}
_CHECKS = {"interval", "cpu", "memory", "swap", "load", "disks", "http",
           "systemd", "docker"}
_THRESHOLD = {"enabled", "warn", "crit"}
_DISK = {"path", "warn", "crit"}
_HTTP = {"name", "url", "expect_status", "timeout"}
_DOCKER = {"enabled", "containers", "compose"}
_ALERTS = {"confirm", "hysteresis", "remind_after", "group_window", "quiet_hours"}
_QUIET = {"start", "end"}
_LOGS = {"enabled", "notify", "level", "digest_interval", "retention_days",
         "max_rows", "sources"}
_SOURCE = {"type", "name", "pattern", "path", "unit", "container", "compose",
           "service"}
_REPORT = {"enabled", "interval", "disks", "interfaces"}

_SOURCE_TYPES = {"file", "journal", "docker_container", "docker"}
_LEVELS = {"all", "info", "warn", "error"}


@dataclass(frozen=True)
class Problem:
    level: str          # "error" | "warning"
    path: str           # e.g. "checks.cpu" or "logs.sources[1].pattern"
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.path}: {self.message}"


class _Report:
    def __init__(self) -> None:
        self.items: list[Problem] = []

    def error(self, path: str, msg: str) -> None:
        self.items.append(Problem("error", path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.items.append(Problem("warning", path, msg))

    def unknown_keys(self, path: str, raw: dict, known: set[str]) -> None:
        for key in raw:
            if key not in known:
                self.warn(f"{path}.{key}" if path else str(key),
                          "unknown key (typo? it is ignored)")


def _number(r: _Report, path: str, value, *, minimum: float | None = None,
            positive: bool = False) -> float | None:
    """A number the parser will accept, or None. An explicit YAML null means
    "use the default" to the parser, so it is not an error here either."""
    if value is None:
        return None
    if isinstance(value, bool):
        r.error(path, f"expected a number, got {value!r}")
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        r.error(path, f"expected a number, got {value!r}")
        return None
    if positive and num <= 0:
        r.error(path, "must be greater than 0")
        return None
    if minimum is not None and num < minimum:
        r.error(path, f"must be at least {minimum:g}")
        return None
    return num


def _section(r: _Report, path: str, value, known: set[str]) -> dict | None:
    """Sections may be omitted or null (`logs:` with nothing under it)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        r.error(path, "expected a mapping")
        return None
    r.unknown_keys(path, value, known)
    return value


def _pair(r: _Report, path: str, raw: dict, *, percent: bool,
          default_warn: float, default_crit: float) -> None:
    warn = _number(r, f"{path}.warn", raw.get("warn"))
    crit = _number(r, f"{path}.crit", raw.get("crit"))
    if raw.get("warn") is None:
        warn = default_warn
    if raw.get("crit") is None:
        crit = default_crit
    if warn is None or crit is None:       # a bad value, already reported
        return
    if warn >= crit:
        r.error(path, f"warn ({warn:g}) must be lower than crit ({crit:g})")
    if percent and (warn > 100 or crit > 100):
        r.warn(path, "a percentage above 100 can never trigger")
    if warn <= 0:
        r.warn(path, "warn is 0 or negative — this will always alert")


def _threshold(r: _Report, path: str, value, *, percent: bool,
               default_warn: float, default_crit: float) -> None:
    if value is None or isinstance(value, bool):
        return
    raw = _section(r, path, value, _THRESHOLD)
    if raw is not None:
        _pair(r, path, raw, percent=percent,
              default_warn=default_warn, default_crit=default_crit)


def _regex(r: _Report, path: str, pattern) -> None:
    try:
        re.compile(str(pattern))
    except re.error as e:
        r.error(path, f"invalid regular expression: {e}")


def _checks(r: _Report, raw: dict) -> None:
    if "interval" in raw:
        _number(r, "checks.interval", raw["interval"], positive=True)
    _threshold(r, "checks.cpu", raw.get("cpu"), percent=True,
               default_warn=80, default_crit=90)
    _threshold(r, "checks.memory", raw.get("memory"), percent=True,
               default_warn=80, default_crit=90)
    _threshold(r, "checks.swap", raw.get("swap"), percent=True,
               default_warn=50, default_crit=80)
    _threshold(r, "checks.load", raw.get("load"), percent=False,
               default_warn=2, default_crit=4)

    disks = raw.get("disks")
    if disks is not None and not isinstance(disks, list):
        r.error("checks.disks", "expected a list")
    for i, item in enumerate(disks if isinstance(disks, list) else []):
        path = f"checks.disks[{i}]"
        if isinstance(item, str):
            disk_path = item
        elif isinstance(item, dict):
            r.unknown_keys(path, item, _DISK)
            disk_path = str(item.get("path", "/"))
            _pair(r, path, item, percent=True, default_warn=80, default_crit=90)
        else:
            r.error(path, "expected a path or a mapping")
            continue
        if not disk_path.startswith("/"):
            r.warn(path, f"{disk_path!r} is not an absolute path")

    http = raw.get("http")
    if http is not None and not isinstance(http, list):
        r.error("checks.http", "expected a list")
    seen: set[str] = set()
    for i, item in enumerate(http if isinstance(http, list) else []):
        path = f"checks.http[{i}]"
        if isinstance(item, str):
            url, name = item, item
        elif isinstance(item, dict):
            r.unknown_keys(path, item, _HTTP)
            url = str(item.get("url") or "")
            name = str(item.get("name") or url)
            if "expect_status" in item:
                status = _number(r, f"{path}.expect_status", item["expect_status"])
                if status is not None and not 100 <= status <= 599:
                    r.error(f"{path}.expect_status", "must be an HTTP status (100-599)")
            if "timeout" in item:
                _number(r, f"{path}.timeout", item["timeout"], positive=True)
        else:
            r.error(path, "expected a URL or a mapping")
            continue
        if not url:
            r.error(path, "`url` is required (this entry is skipped)")
            continue
        if not re.match(r"^https?://", url):
            r.error(path, f"url {url!r} must start with http:// or https://")
        if name in seen:
            r.warn(path, f"duplicate http name {name!r} — the checks share one state")
        seen.add(name)

    systemd = raw.get("systemd")
    if systemd is not None and not isinstance(systemd, list):
        r.error("checks.systemd", "expected a list")

    docker = _section(r, "checks.docker", raw.get("docker"), _DOCKER)
    if docker:
        for key in ("containers", "compose"):
            if docker.get(key) is not None and not isinstance(docker[key], list):
                r.error(f"checks.docker.{key}", "expected a list")


def _alerts(r: _Report, raw: dict) -> None:
    if "confirm" in raw:
        n = _number(r, "alerts.confirm", raw["confirm"], minimum=1)
        if n is not None and n != int(n):
            r.error("alerts.confirm", "must be a whole number")
    if "hysteresis" in raw:
        h = _number(r, "alerts.hysteresis", raw["hysteresis"], minimum=0)
        if h is not None and h > 50:
            r.warn("alerts.hysteresis", "values above 50 are capped at 50")
    if "remind_after" in raw:
        _number(r, "alerts.remind_after", raw["remind_after"], minimum=0)
    if "group_window" in raw:
        g = _number(r, "alerts.group_window", raw["group_window"], minimum=0)
        if g is not None and g > 60:
            r.warn("alerts.group_window", "values above 60 are capped at 60")

    quiet = raw.get("quiet_hours")
    if quiet is None or quiet is False:
        return
    if not isinstance(quiet, dict):
        r.error("alerts.quiet_hours", "expected {start: \"23:00\", end: \"07:00\"}")
        return
    r.unknown_keys("alerts.quiet_hours", quiet, _QUIET)
    times = {}
    for key in ("start", "end"):
        value = parse_clock(quiet.get(key))
        if value is None:
            r.error(f"alerts.quiet_hours.{key}",
                    f"expected a time like \"23:00\", got {quiet.get(key)!r}")
        times[key] = value
    if times["start"] is not None and times["start"] == times["end"]:
        r.error("alerts.quiet_hours", "start and end are the same — the window is empty")


def _logs(r: _Report, raw: dict) -> None:
    level = str(raw.get("level", "error")).lower()
    if level not in _LEVELS:
        r.warn("logs.level",
               f"unknown level {level!r} — falls back to 'all' "
               f"(use one of: {', '.join(sorted(_LEVELS))})")
    for key in ("digest_interval", "retention_days", "max_rows"):
        if key in raw:
            _number(r, f"logs.{key}", raw[key], positive=True)

    sources = raw.get("sources")
    if sources is not None and not isinstance(sources, list):
        r.error("logs.sources", "expected a list")
        return
    names: set[str] = set()
    for i, item in enumerate(sources or []):
        path = f"logs.sources[{i}]"
        if not isinstance(item, dict):
            r.error(path, "expected a mapping (this entry is skipped)")
            continue
        r.unknown_keys(path, item, _SOURCE)
        stype = str(item.get("type", "file"))
        needs = {
            "file": ["path"], "journal": ["unit"],
            "docker_container": ["container"], "docker": ["compose", "service"],
        }
        if stype not in _SOURCE_TYPES:
            r.error(f"{path}.type", f"unknown source type {stype!r} "
                    f"(use one of: {', '.join(sorted(_SOURCE_TYPES))})")
        else:
            for field_name in needs[stype]:
                if not item.get(field_name):
                    r.error(f"{path}.{field_name}",
                            f"`{field_name}` is required for a {stype} source")
        if "pattern" in item:
            _regex(r, f"{path}.pattern", item["pattern"])
        name = str(item.get("name") or item.get("container") or item.get("unit")
                   or item.get("service") or item.get("path") or stype)
        if name in names:
            r.error(f"{path}.name",
                    f"duplicate source name {name!r} — this source is skipped")
        names.add(name)


def validate(raw) -> list[Problem]:
    r = _Report()
    try:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            r.error("", "the config must be a YAML mapping at the top level")
            return r.items
        r.unknown_keys("", raw, _TOP)

        tg = _section(r, "telegram", raw.get("telegram"), _TELEGRAM)
        if not tg or not (tg.get("bot_token") and tg.get("chat_id")):
            r.warn("telegram", "bot_token and chat_id are not both set — "
                   "alerts will only go to the log")
        if tg and str(tg.get("lang", "en")).lower()[:2] not in ("en", "ru"):
            r.warn("telegram.lang", f"unsupported language {tg.get('lang')!r} "
                   "— falls back to en")

        if tg is not None:
            admins = tg.get("admins")
            if admins is not None and not isinstance(admins, list):
                r.error("telegram.admins", "expected a list of Telegram user ids")
            elif admins and any(isinstance(a, bool) or not str(a).lstrip("-").isdigit()
                                for a in admins):
                r.error("telegram.admins", "user ids are numbers (ask @userinfobot)")
            if tg.get("interactive") is not False and not tg.get("admins") \
                    and str(tg.get("chat_id", "")).startswith("-"):
                r.warn("telegram.admins",
                       "this is a group chat and `admins` is empty — anyone in "
                       "the group can mute and acknowledge alerts")

        checks = _section(r, "checks", raw.get("checks"), _CHECKS)
        if checks:
            _checks(r, checks)
        alerts = _section(r, "alerts", raw.get("alerts"), _ALERTS)
        if alerts:
            _alerts(r, alerts)
        logs = _section(r, "logs", raw.get("logs"), _LOGS)
        if logs:
            _logs(r, logs)
        report = _section(r, "report", raw.get("report"), _REPORT)
        if report and "interval" in report:
            _number(r, "report.interval", report["interval"], positive=True)
    except Exception as e:      # pragma: no cover - the safety net
        r.error("", f"validation itself failed: {type(e).__name__}: {e}")
    return r.items


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == "error"]
