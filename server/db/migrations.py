"""First-run migrations from config.yaml → DB.

Runs once after `init_db` on every boot. Idempotent — only writes when
the target rows are missing, so re-running on an existing install does
nothing. Two cases:

  - **Settings singleton** (`id=1`): created once from the YAML's
    notifiers / checks / report / logs / web.terminal blocks. After
    that the DB is the source of truth for those sections; the YAML
    is consulted only for boot-time things (db.path, web.host/port/
    prefix, web.jwt.secret).

  - **First admin user**: imported from `web.user.{username,
    password_hash}` if the `users` table is empty. Operator can
    edit the table afterwards (add more, demote/disable, change
    passwords) without touching config.yaml.
"""

import logging
import time

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import Config
from db.models import Settings, User

log = logging.getLogger(__name__)


async def import_yaml_into_db(
    session_maker: async_sessionmaker,
    config: Config,
) -> None:
    """Bootstrap the DB from `config.yaml` on first run.

    Returns silently when the DB is already populated.
    """
    async with session_maker() as session:  # type: AsyncSession
        await _ensure_settings(session, config)
        await _ensure_first_admin(session, config)
        await session.commit()


async def _ensure_settings(session: AsyncSession, config: Config) -> None:
    existing = (await session.exec(select(Settings).where(Settings.id == 1))).first()
    if existing is not None:
        return
    web = config.web or {}
    row = Settings(
        id=1,
        # `config.notifiers` was parsed into NotifierConfig objects; for
        # the JSON column we store a clean dict per item.
        notifiers=[
            {"type": n.type, **n.options} for n in (config.notifiers or [])
        ],
        # Same for checks — flatten back to plain dicts.
        checks=[
            {"type": c.type, "name": c.name, "interval": c.interval, **c.options}
            for c in (config.checks or [])
        ],
        report=config.report,
        logs=config.logs,
        terminal=web.get("terminal"),
        updated_at=time.time(),
    )
    session.add(row)
    log.info("settings: imported from config.yaml")


async def _ensure_first_admin(session: AsyncSession, config: Config) -> None:
    any_user = (await session.exec(select(User).limit(1))).first()
    if any_user is not None:
        return
    web = config.web or {}
    user_cfg = web.get("user") or {}
    username = user_cfg.get("username")
    password_hash = user_cfg.get("password_hash")
    if not username or not password_hash:
        log.warning(
            "users: no `web.user` in config.yaml — skipping first-admin "
            "seed; nobody can log in until a user is created via the API "
            "or you re-run `make setup`.",
        )
        return
    session.add(User(
        username=username,
        password_hash=password_hash,
        role="admin",
        is_active=True,
        created_at=time.time(),
    ))
    log.info("users: seeded first admin user %r from config.yaml", username)
