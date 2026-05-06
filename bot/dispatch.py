"""Background fan-out: poll source tables and DM new events to subscribers.

Why polling instead of a hook: the daemon process and the bot process are
separate. A direct callback would couple them. Polling the SQLite tables
every few seconds is cheap (indexed `id > :since` queries) and survives
either side restarting independently.

State (last-seen ids) lives in `data/bot_dispatch.json` so a bot restart
doesn't replay every old alert. On first run it starts from the current
max id — past events stay un-pushed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlmodel import desc, select

from db.models import AlertEvent, CheckResult, LogEvent  # type: ignore

from .db import db_session
from .format import esc, level_emoji, short_dt
from .subs import prune_user, subscribers_for


log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0  # seconds — `dispatch` loop tick
BATCH = 50           # max rows per poll, per source — bounds the loop
STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "bot_dispatch.json"


# ── persistence of last-seen ids ──────────────────────────────────────────


def _load_state() -> dict[str, int]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), "utf-8")
    tmp.replace(STATE_FILE)


def _max_id(model) -> int:
    with db_session() as s:
        r = s.exec(select(model.id).order_by(desc(model.id)).limit(1)).first()
        return int(r or 0)


# ── safe send ─────────────────────────────────────────────────────────────


async def _send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramForbiddenError:
        # User blocked the bot. Drop their subscriptions so we stop trying.
        log.info("subscriber %s blocked the bot — pruning subs", chat_id)
        prune_user(chat_id)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.warning("retry-after send failed for %s", chat_id, exc_info=True)
    except Exception:
        log.warning("send failed for %s", chat_id, exc_info=True)


# ── per-topic dispatch ────────────────────────────────────────────────────


async def _dispatch_alerts(bot: Bot, since_id: int) -> int:
    with db_session() as s:
        rows = s.exec(
            select(AlertEvent)
            .where(AlertEvent.id > since_id)
            .order_by(AlertEvent.id)
            .limit(BATCH)
        ).all()
        rows = [r.model_copy() for r in rows]
    if not rows:
        return since_id

    for r in rows:
        text = (
            f"{level_emoji(r.level)} <b>alert</b>: <code>{esc(r.name)}</code>\n"
            f"<i>{short_dt(r.ts)}</i>  <code>{esc(r.level)}</code>\n"
            f"{esc(r.detail or '')}"
        )
        for chat_id in subscribers_for("alerts", r.name):
            await _send(bot, chat_id, text)
    return rows[-1].id


async def _dispatch_logs(bot: Bot, since_id: int) -> int:
    with db_session() as s:
        rows = s.exec(
            select(LogEvent)
            .where(LogEvent.id > since_id)
            .where(LogEvent.first == True)   # only first occurrence per signature
            .order_by(LogEvent.id)
            .limit(BATCH)
        ).all()
        rows = [r.model_copy() for r in rows]
    if not rows:
        return since_id

    for r in rows:
        line = r.line.strip()
        if len(line) > 200:
            line = line[:200] + "…"
        text = (
            f"📝 <b>log</b>: <code>{esc(r.source)}</code>  "
            f"<i>{short_dt(r.ts)}</i>\n"
            f"<i>{esc(line)}</i>"
        )
        for chat_id in subscribers_for("logs", r.source):
            await _send(bot, chat_id, text)
    return rows[-1].id


async def _dispatch_checks(bot: Bot, since_id: int) -> int:
    with db_session() as s:
        rows = s.exec(
            select(CheckResult)
            .where(CheckResult.id > since_id)
            .order_by(CheckResult.id)
            .limit(BATCH)
        ).all()
        rows = [r.model_copy() for r in rows]
    if not rows:
        return since_id

    for r in rows:
        # Per-check is high-frequency — only push if anyone explicitly
        # subscribed to THIS check's name. No catch-all.
        chats = subscribers_for("checks", r.name)
        if not chats:
            continue
        val = (r.metrics or {}).get("value")
        val_str = f"  <code>{val:.1f}</code>" if isinstance(val, (int, float)) else ""
        text = (
            f"{level_emoji(r.level)} <b>{esc(r.name)}</b>{val_str}  "
            f"<code>{esc(r.level)}</code>\n"
            f"<i>{esc(r.detail or '')}</i>"
        )
        for chat_id in chats:
            await _send(bot, chat_id, text)
    return rows[-1].id


# ── main loop ─────────────────────────────────────────────────────────────


async def run_dispatcher(bot: Bot) -> None:
    """Run forever, polling tables every POLL_INTERVAL seconds."""
    state = _load_state()
    state.setdefault("alerts_id", _max_id(AlertEvent))
    state.setdefault("logs_id", _max_id(LogEvent))
    state.setdefault("checks_id", _max_id(CheckResult))
    _save_state(state)
    log.info("dispatcher start: alerts>%d logs>%d checks>%d",
             state["alerts_id"], state["logs_id"], state["checks_id"])

    while True:
        try:
            state["alerts_id"] = await _dispatch_alerts(bot, state["alerts_id"])
            state["logs_id"] = await _dispatch_logs(bot, state["logs_id"])
            state["checks_id"] = await _dispatch_checks(bot, state["checks_id"])
            _save_state(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dispatcher tick failed — sleeping then retrying")
        await asyncio.sleep(POLL_INTERVAL)
