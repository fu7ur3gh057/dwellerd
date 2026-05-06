"""bot_subscriptions CRUD + lookup helpers.

Subscriptions are tiny (one row per topic-filter pair per TG user). The
runtime dispatcher (bot/dispatch.py) polls source tables and uses these
helpers to fan out to interested chats.
"""
from __future__ import annotations

import time

from sqlmodel import select

from db.models import BotSubscription  # type: ignore

from .db import db_session


VALID_TOPICS = {"alerts", "logs", "checks"}


def add(tg_user_id: int, topic: str, filter: str | None) -> bool:
    """Return True on insert, False if the subscription already exists."""
    if topic not in VALID_TOPICS:
        raise ValueError(f"unknown topic: {topic}")
    with db_session() as s:
        rows = s.exec(
            select(BotSubscription)
            .where(BotSubscription.tg_user_id == tg_user_id)
            .where(BotSubscription.topic == topic)
        ).all()
        for r in rows:
            if r.filter == filter:
                return False
        s.add(BotSubscription(
            tg_user_id=tg_user_id, topic=topic, filter=filter,
            created_at=time.time(),
        ))
        s.commit()
        return True


def remove(tg_user_id: int, topic: str | None = None) -> int:
    """Drop subscriptions for this user. When `topic` is None, drop all.
    Returns the count removed.
    """
    with db_session() as s:
        q = select(BotSubscription).where(BotSubscription.tg_user_id == tg_user_id)
        if topic:
            q = q.where(BotSubscription.topic == topic)
        rows = s.exec(q).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def list_for(tg_user_id: int) -> list[BotSubscription]:
    with db_session() as s:
        return [
            r.model_copy() for r in s.exec(
                select(BotSubscription)
                .where(BotSubscription.tg_user_id == tg_user_id)
                .order_by(BotSubscription.topic, BotSubscription.filter)
            ).all()
        ]


def subscribers_for(topic: str, name: str | None = None) -> list[int]:
    """Return TG user ids subscribed to `topic` whose filter matches `name`
    (or has no filter — meaning 'all events of this topic').
    """
    with db_session() as s:
        rows = s.exec(
            select(BotSubscription).where(BotSubscription.topic == topic)
        ).all()
        return [
            r.tg_user_id for r in rows
            if r.filter is None or r.filter == name
        ]


def prune_user(tg_user_id: int) -> None:
    """Drop all subs for a TG user — used when they block the bot."""
    remove(tg_user_id, topic=None)
