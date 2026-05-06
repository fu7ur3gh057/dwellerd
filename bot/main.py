"""Bot entrypoint — builds the Bot + Dispatcher, registers routers and
middlewares, runs polling. Graceful shutdown on SIGINT/SIGTERM.

Run with:
    python -m bot
or:
    make run-bot
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_config
from .db import init_db
from .dispatch import run_dispatcher
from .handlers import register_routers
from .middlewares import register_middlewares


log = logging.getLogger("dwellerd.bot")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # aiogram is chatty at DEBUG — keep it at INFO unless we explicitly need
    # the wire-level dump.
    logging.getLogger("aiogram").setLevel(logging.INFO)


async def run() -> None:
    _setup_logging()
    cfg = load_config()
    # Open the SQLite DB once at startup so the first /login query
    # doesn't pay the table-create + WAL-pragma cost.
    init_db()

    bot = Bot(
        token=cfg.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Inject config into handler context so handlers don't need to re-read it.
    dp["config"] = cfg

    register_middlewares(dp)
    register_routers(dp)

    me = await bot.get_me()
    log.info("bot ready: @%s (id=%s)", me.username, me.id)

    # Background fan-out: poll DB tables, push new alerts/logs/checks to
    # subscribed TG users. Runs alongside polling, cancelled on shutdown.
    dispatcher_task = asyncio.create_task(run_dispatcher(bot), name="dispatcher")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        dispatcher_task.cancel()
        try:
            await dispatcher_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
