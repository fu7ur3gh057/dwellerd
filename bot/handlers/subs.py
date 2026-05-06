"""/subscribe /unsubscribe /subscriptions — push topic config per TG user."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from .. import subs
from ..format import esc


router = Router(name=__name__)


_USAGE_SUB = (
    "Использование:\n"
    "<code>/subscribe alerts</code> — все алерты в DM\n"
    "<code>/subscribe alerts &lt;check&gt;</code> — только этой проверки\n"
    "<code>/subscribe logs</code> — все ERROR-логи (только первое появление сигнатуры)\n"
    "<code>/subscribe logs &lt;source&gt;</code> — только этого источника\n"
    "<code>/subscribe checks &lt;name&gt;</code> — каждый результат конкретной проверки"
)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await message.answer(_USAGE_SUB)
        return
    topic = parts[0].lower()
    filter_ = parts[1].strip() if len(parts) > 1 else None

    if topic not in subs.VALID_TOPICS:
        await message.answer(
            f"Неизвестный topic <code>{esc(topic)}</code>. Доступны: "
            f"{', '.join(sorted(subs.VALID_TOPICS))}"
        )
        return
    if topic == "checks" and filter_ is None:
        await message.answer(
            "Для <code>checks</code> нужно указать имя проверки — "
            "/subscribe checks <code>cpu</code>"
        )
        return

    try:
        added = subs.add(message.from_user.id, topic, filter_)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    label = f"{topic}" + (f" · {filter_}" if filter_ else "")
    if added:
        await message.answer(f"🔔 Подписан: <code>{esc(label)}</code>")
    else:
        await message.answer(f"Уже подписан: <code>{esc(label)}</code>")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    arg = (command.args or "").strip().lower()
    if not arg:
        n = subs.remove(message.from_user.id)
        await message.answer(f"Снял подписки: {n}")
        return
    if arg in subs.VALID_TOPICS:
        n = subs.remove(message.from_user.id, topic=arg)
        await message.answer(f"Снял подписки на <code>{arg}</code>: {n}")
        return
    await message.answer(
        f"Используй: <code>/unsubscribe</code> (всё) или "
        f"<code>/unsubscribe &lt;{'/'.join(sorted(subs.VALID_TOPICS))}&gt;</code>"
    )


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message) -> None:
    if message.from_user is None:
        return
    rows = subs.list_for(message.from_user.id)
    if not rows:
        await message.answer("Подписок нет. /subscribe чтобы добавить.")
        return
    lines = ["<b>Subscriptions</b>"]
    for r in rows:
        label = r.topic + (f" · {r.filter}" if r.filter else " · *")
        lines.append(f"  🔔 <code>{esc(label)}</code>")
    await message.answer("\n".join(lines))
