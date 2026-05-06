"""Broker startup/shutdown wiring.

`init_broker` brings up the engine, runs the YAML→DB migration once,
re-reads runtime settings from the DB into the in-memory `Config`, then
builds the AppContext from that. config.yaml stays as boot-only seed +
escape hatch (db.path, web.host/port/prefix, web.jwt.secret).

`shutdown_broker` is the symmetric cleanup. Both are idempotent enough
that calling shutdown without a prior startup is harmless.
"""

import logging
from pathlib import Path

from sqlmodel import select

from core.checks import build_check
from config import CheckConfig, Config, NotifierConfig
from core.logs import build_log_store
from core.notifiers import build_notifier
from core.report import build_report_context
from db.lifetime import init_db, shutdown_db
from db.migrations import import_yaml_into_db
from db.models import Settings
from services.taskiq.broker import broker
from services.taskiq.context import AppContext

log = logging.getLogger(__name__)


def _resolve_db_path(config: Config) -> Path:
    """Pick the SQLite file path. Priority:
      1. `db.path` from config.yaml (operator override)
      2. `<config.data_dir>/dwellerd.sqlite` (auto: prod → /var/lib/dwellerd/data/,
         dev → ./data/, depending on where config.yaml itself lives)
    """
    explicit = (config.db or {}).get("path")
    if explicit:
        return Path(explicit)
    return config.data_dir / "dwellerd.sqlite"


async def init_broker(config: Config) -> AppContext:
    db_path = _resolve_db_path(config)
    await init_db(db_path)

    # Bootstrap from config.yaml on first run; no-op afterwards.
    sm = broker.state.data["db_session_maker"]
    await import_yaml_into_db(sm, config)

    # Hydrate runtime config from the DB Settings row. After this point
    # config.notifiers / .checks / .report / .logs reflect the DB, not
    # the YAML.
    await _hydrate_config_from_db(config)

    notifiers_by_type = {n.type: build_notifier(n) for n in config.notifiers}
    notifiers = list(notifiers_by_type.values())

    ctx = AppContext(
        config=config,
        checks_by_name={c.name: build_check(c) for c in config.checks},
        notifiers_by_type=notifiers_by_type,
        notifiers=notifiers,
    )

    if config.logs:
        ctx.logs_enabled = True
        broker.state.log_store = build_log_store(config.logs)

    if config.report:
        report_ctx = build_report_context(
            config.report,
            notifiers_by_type,
            logs_enabled=ctx.logs_enabled,
        )
        if report_ctx is not None:
            ctx.report_hostname = report_ctx["hostname"]
            ctx.report_lang = report_ctx["lang"]
            ctx.report_sections = report_ctx["sections"]
            ctx.report_targets = report_ctx["targets"]

    broker.state.app_ctx = ctx
    await broker.startup()
    log.info(
        "broker ready: %d checks, %d notifiers, broker=%s",
        len(ctx.checks_by_name), len(ctx.notifiers), type(broker).__name__,
    )
    return ctx


async def _hydrate_config_from_db(config: Config) -> None:
    """Pull notifiers / checks / report / logs / web.terminal from the
    Settings row and replace the corresponding fields in `config`. Boot-
    only sections (db, web.host/port/jwt/prefix) stay untouched."""
    sm = broker.state.data["db_session_maker"]
    async with sm() as session:
        row = (await session.exec(select(Settings).where(Settings.id == 1))).first()
    if row is None:
        log.warning("settings: no Settings row — falling back to config.yaml values")
        return

    # Notifiers — convert {type, enabled?, ...rest} dicts → NotifierConfig.
    # `enabled` defaults to True so legacy rows (without the field) keep
    # firing exactly as before; the Settings UI flips it explicitly.
    if row.notifiers is not None:
        out_n: list[NotifierConfig] = []
        for item in row.notifiers:
            item = dict(item)
            t = item.pop("type", "")
            enabled = item.pop("enabled", True) is not False
            out_n.append(NotifierConfig(type=t, options=item, enabled=enabled))
        config.notifiers = out_n

    # Checks — same idea, with the standard fields broken out.
    if row.checks is not None:
        out: list[CheckConfig] = []
        for item in row.checks:
            item = dict(item)
            t = item.pop("type", "")
            n = item.pop("name", "")
            interval = float(item.pop("interval", 60))
            enabled = item.pop("enabled", True) is not False
            out.append(CheckConfig(
                type=t, name=n, interval=interval, options=item, enabled=enabled,
            ))
        config.checks = out

    # Report + logs are stored as-is.
    if row.report is not None:
        config.report = row.report
    if row.logs is not None:
        config.logs = row.logs

    # web.terminal is part of the DB; merge into the existing web dict
    # so JWT secret / host / port stay available.
    if row.terminal is not None:
        web = config.web or {}
        web["terminal"] = row.terminal
        config.web = web


async def shutdown_broker() -> None:
    try:
        await broker.shutdown()
    except Exception:
        log.exception("broker shutdown failed")
    await shutdown_db()
