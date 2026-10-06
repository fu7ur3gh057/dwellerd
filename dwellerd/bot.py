"""The interactive half of the Telegram integration: the buttons under an
alert and a few slash commands.

Everything else in this daemon only pushes. This module is the one place
that listens, by long-polling `getUpdates`: no webhook, no open port, the
same outbound HTTPS the notifier already uses. It can be switched off with
`telegram.interactive: false`.

What it can do — and, deliberately, nothing more:

    buttons      🔕 Mute 1h / 8h   on any warn or crit alert and on new-error
                 👍 Got it         alerts; Got it only on crit
    /mute 2h [pattern]    silence alerts for a while
    /unmute [pattern]     lift one mute, or all
    /mutes                what is muted, and until when
    /ack <check>          stop reminding me about this problem
    /help

Safety rules, because this is a way to change the daemon's behaviour from
the network:

  - Only updates from the configured chat are acted on. Anything else is
    ignored (commands) or refused (buttons) and logged.
  - `telegram.admins` narrows that to specific user ids, for group chats.
  - Backlog is skipped at startup. A button pressed while the daemon was
    down is not replayed minutes or hours later.
  - It acts on the daemon's own state (mutes, acknowledgements). It cannot
    run commands on the host.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import time

from .duration import parse_duration
from .i18n import fmt_duration, normalize_lang
from .notify.telegram import esc

log = logging.getLogger(__name__)

_POLL_TIMEOUT = 25          # seconds Telegram holds a getUpdates open
_MAX_BACKOFF = 60.0
_CONFLICT_DELAY = 60.0

_TEXT = {
    "en": {
        "denied": "Not allowed",
        "expired": "This button has expired",
        "muted": "Muted {name} for {dur}",
        "muted_btn": "🔕 Muted until {until}",
        "acked": "Got it — no more reminders for {name}",
        "acked_btn": "👍 Acknowledged",
        "ack_ok": "{name} is already back to normal",
        "ack_unknown": "No such check: {name}",
        "ack_usage": "Usage: <code>/ack &lt;check&gt;</code>",
        "ack_list": "Not ok right now:",
        "ack_none": "Nothing is wrong right now.",
        "mute_usage": "Usage: <code>/mute 2h [check or pattern]</code>\n"
                      "Durations: 30m, 2h, 1d, 1h30m. Pattern: <code>cpu</code>, "
                      "<code>docker:*</code>, <code>log:*</code>; default is everything.",
        "mute_done": "🔕 Muted <code>{pattern}</code> until <b>{until}</b>",
        "unmute_done": "🔔 Unmuted <code>{pattern}</code>",
        "unmute_all": "🔔 All mutes lifted",
        "unmute_none": "Nothing to unmute for <code>{pattern}</code>",
        "mutes_none": "No active mutes.",
        "mutes_head": "🔕 Active mutes:",
        "mutes_row": "<code>{pattern}</code> — until {until} ({left} left)",
        "help": "<b>Dwellerd</b>\n"
                "/mute <code>2h [pattern]</code> — silence alerts\n"
                "/unmute <code>[pattern]</code> — lift a mute\n"
                "/mutes — list active mutes\n"
                "/ack <code>&lt;check&gt;</code> — stop reminders for a problem\n"
                "/help — this message",
        "bad_duration": "Cannot read <code>{text}</code> as a duration — use 30m, 2h, 1d or 1h30m.",
        "cmd_mute": "Silence alerts for a while",
        "cmd_unmute": "Lift a mute",
        "cmd_mutes": "List active mutes",
        "cmd_ack": "Stop reminders for a problem",
        "cmd_help": "What I can do",
    },
    "ru": {
        "denied": "Нет доступа",
        "expired": "Эта кнопка устарела",
        "muted": "Заглушено: {name} на {dur}",
        "muted_btn": "🔕 Тихо до {until}",
        "acked": "Принято — напоминаний про {name} больше не будет",
        "acked_btn": "👍 Принято",
        "ack_ok": "{name} уже в норме",
        "ack_unknown": "Нет такой проверки: {name}",
        "ack_usage": "Использование: <code>/ack &lt;проверка&gt;</code>",
        "ack_list": "Сейчас не в порядке:",
        "ack_none": "Сейчас всё в порядке.",
        "mute_usage": "Использование: <code>/mute 2h [проверка или шаблон]</code>\n"
                      "Длительность: 30m, 2h, 1d, 1h30m. Шаблон: <code>cpu</code>, "
                      "<code>docker:*</code>, <code>log:*</code>; по умолчанию всё.",
        "mute_done": "🔕 Заглушено <code>{pattern}</code> до <b>{until}</b>",
        "unmute_done": "🔔 Снова слышно <code>{pattern}</code>",
        "unmute_all": "🔔 Все заглушки сняты",
        "unmute_none": "Для <code>{pattern}</code> нечего снимать",
        "mutes_none": "Активных заглушек нет.",
        "mutes_head": "🔕 Активные заглушки:",
        "mutes_row": "<code>{pattern}</code> — до {until} (осталось {left})",
        "help": "<b>Dwellerd</b>\n"
                "/mute <code>2h [шаблон]</code> — заглушить алерты\n"
                "/unmute <code>[шаблон]</code> — снять заглушку\n"
                "/mutes — активные заглушки\n"
                "/ack <code>&lt;проверка&gt;</code> — не напоминать о проблеме\n"
                "/help — эта справка",
        "bad_duration": "Не понимаю <code>{text}</code> как длительность — "
                        "используйте 30m, 2h, 1d или 1h30m.",
        "cmd_mute": "Заглушить алерты на время",
        "cmd_unmute": "Снять заглушку",
        "cmd_mutes": "Активные заглушки",
        "cmd_ack": "Не напоминать о проблеме",
        "cmd_help": "Что я умею",
    },
}


def _until(ts: float) -> str:
    """Local clock time, with the date once it is not today-ish."""
    fmt = "%H:%M" if ts - time.time() < 20 * 3600 else "%Y-%m-%d %H:%M"
    return time.strftime(fmt, time.localtime(ts))


class Bot:
    def __init__(
        self, notifier, storage, *, chat_id: str, admins: list[str] | None = None,
        lang: str = "en",
    ) -> None:
        self.notifier = notifier
        self.storage = storage
        self.chat_id = str(chat_id)
        self.admins = {str(a) for a in (admins or [])}
        self.t = _TEXT[normalize_lang(lang)]
        self.lang = normalize_lang(lang)

    # ── authorisation ────────────────────────────────────────────────────

    def allowed(self, chat_id, user_id) -> bool:
        if str(chat_id) != self.chat_id:
            return False
        return not self.admins or str(user_id) in self.admins

    # ── polling ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Poll until cancelled. Network trouble backs off and retries; a
        rejected token ends the loop (retrying cannot fix it) and leaves the
        rest of the daemon running."""
        n = self.notifier
        async with n.http_client(_POLL_TIMEOUT + 15) as client:
            await self._register_commands(client)
            offset: int | None = None
            synced = False
            backoff = 1.0
            while True:
                if not synced:
                    # Drop whatever piled up while we were not running.
                    status, body = await n.api_call(
                        "getUpdates", {"offset": -1, "limit": 1, "timeout": 0},
                        client=client,
                    )
                    if status == 200 and body.get("ok"):
                        last = body.get("result") or []
                        if last:
                            offset = last[-1]["update_id"] + 1
                        synced, backoff = True, 1.0
                        log.info("telegram buttons and commands are on")
                        continue
                else:
                    payload = {
                        "timeout": _POLL_TIMEOUT,
                        "allowed_updates": ["message", "callback_query"],
                    }
                    if offset is not None:
                        payload["offset"] = offset
                    status, body = await n.api_call(
                        "getUpdates", payload, client=client,
                    )
                    if status == 200 and body.get("ok"):
                        for update in body.get("result") or []:
                            offset = update["update_id"] + 1
                            try:
                                await self.handle_update(update)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                log.exception("telegram update failed")
                        backoff = 1.0
                        continue

                if status in (401, 404):
                    log.error("telegram rejected the bot token (%s) — buttons "
                              "and commands are off", status)
                    return
                if status == 409:
                    log.error("telegram: another process is polling this bot "
                              "token (or a webhook is set) — buttons and commands "
                              "wait; retrying in %.0fs", _CONFLICT_DELAY)
                    delay = _CONFLICT_DELAY
                else:
                    delay = backoff
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                await asyncio.sleep(delay)

    async def _register_commands(self, client) -> None:
        """Fill in Telegram's `/` menu. Cosmetic, so failures are ignored."""
        t = self.t
        commands = [
            {"command": "mute", "description": t["cmd_mute"]},
            {"command": "unmute", "description": t["cmd_unmute"]},
            {"command": "mutes", "description": t["cmd_mutes"]},
            {"command": "ack", "description": t["cmd_ack"]},
            {"command": "help", "description": t["cmd_help"]},
        ]
        await self.notifier.api_call("setMyCommands", {"commands": commands},
                                     client=client)

    # ── updates ──────────────────────────────────────────────────────────

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])

    # ── buttons ──────────────────────────────────────────────────────────

    async def _on_callback(self, cb: dict) -> None:
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (cb.get("from") or {}).get("id")
        cb_id = cb.get("id", "")

        if not self.allowed(chat_id, user_id):
            log.warning("refused a button press from user %s in chat %s",
                        user_id, chat_id)
            await self._answer(cb_id, self.t["denied"])
            return

        data = str(cb.get("data") or "")
        toast, label = await self._run_button(data)
        await self._answer(cb_id, toast)
        if label and message.get("message_id") is not None:
            # Swap the buttons for what happened, so the message shows it
            # and cannot be pressed twice.
            await self.notifier.api_call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message["message_id"],
                "reply_markup": {"inline_keyboard": [[
                    {"text": label, "callback_data": "n"},
                ]]},
            })

    async def _run_button(self, data: str) -> tuple[str, str]:
        """(toast, new button label) for one callback payload."""
        t = self.t
        parts = data.split(":")
        try:
            if parts[0] == "m" and len(parts) == 3:
                target, minutes = int(parts[1]), int(parts[2])
            elif parts[0] == "a" and len(parts) == 2:
                target, minutes = int(parts[1]), 0
            else:
                return "", ""                   # "n", or something unknown
        except ValueError:
            return "", ""

        name = await asyncio.to_thread(self.storage.target_name, target)
        if name is None:
            return t["expired"], ""

        if parts[0] == "a":
            result = await asyncio.to_thread(self.storage.set_acked, name)
            if result == "acked":
                log.info("acknowledged %s", name)
                return t["acked"].format(name=name), t["acked_btn"]
            return t["ack_ok"].format(name=name), ""

        if minutes <= 0 or minutes > 7 * 24 * 60:
            return "", ""               # a forged payload, not one of ours
        until = time.time() + minutes * 60
        await asyncio.to_thread(self.storage.add_mute, glob.escape(name), until)
        log.info("muted %s for %dm from a button", name, minutes)
        return (
            t["muted"].format(name=name, dur=fmt_duration(minutes * 60, self.lang)),
            t["muted_btn"].format(until=_until(until)),
        )

    async def _answer(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self.notifier.api_call("answerCallbackQuery", payload)

    # ── commands ─────────────────────────────────────────────────────────

    async def _on_message(self, msg: dict) -> None:
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = (msg.get("chat") or {}).get("id")
        user_id = (msg.get("from") or {}).get("id")
        if not self.allowed(chat_id, user_id):
            log.warning("ignored a command from user %s in chat %s", user_id, chat_id)
            return
        reply = await self.run_command(text)
        if reply:
            await self.notifier.send_text(reply)

    async def run_command(self, text: str) -> str | None:
        head, _, rest = text.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        args = rest.split()
        handler = {
            "mute": self._cmd_mute, "unmute": self._cmd_unmute,
            "mutes": self._cmd_mutes, "ack": self._cmd_ack,
            "help": self._cmd_help, "start": self._cmd_help,
        }.get(command)
        return await handler(args) if handler else None

    async def _cmd_help(self, args: list[str]) -> str:
        return self.t["help"]

    async def _cmd_mute(self, args: list[str]) -> str:
        t = self.t
        if not args:
            return t["mute_usage"]
        try:
            seconds = parse_duration(args[0])
        except ValueError:
            return t["bad_duration"].format(text=esc(args[0]))
        pattern = " ".join(args[1:]) or "*"
        until = time.time() + seconds
        await asyncio.to_thread(self.storage.add_mute, pattern, until)
        log.info("muted %r for %ds from a command", pattern, seconds)
        return t["mute_done"].format(pattern=esc(pattern), until=_until(until))

    async def _cmd_unmute(self, args: list[str]) -> str:
        t = self.t
        pattern = " ".join(args) or None
        n = await asyncio.to_thread(self.storage.remove_mute, pattern)
        if pattern is None:
            return t["unmute_all"] if n else t["mutes_none"]
        if not n:
            return t["unmute_none"].format(pattern=esc(pattern))
        return t["unmute_done"].format(pattern=esc(pattern))

    async def _cmd_mutes(self, args: list[str]) -> str:
        t = self.t
        mutes = await asyncio.to_thread(self.storage.active_mutes)
        if not mutes:
            return t["mutes_none"]
        now = time.time()
        lines = [t["mutes_head"]] + [
            t["mutes_row"].format(
                pattern=esc(m["pattern"]), until=_until(m["until"]),
                left=fmt_duration(m["until"] - now, self.lang),
            )
            for m in mutes
        ]
        return "\n".join(lines)

    async def _cmd_ack(self, args: list[str]) -> str:
        t = self.t
        if not args:
            bad = await asyncio.to_thread(self.storage.bad_checks)
            if not bad:
                return t["ack_none"]
            lines = [t["ack_usage"], "", t["ack_list"]]
            lines += [f"• <code>{esc(s['name'])}</code>" for s in bad[:30]]
            return "\n".join(lines)
        name = " ".join(args)
        result = await asyncio.to_thread(self.storage.set_acked, name)
        if result == "unknown":
            return t["ack_unknown"].format(name=f"<code>{esc(name)}</code>")
        if result == "ok":
            return t["ack_ok"].format(name=f"<code>{esc(name)}</code>")
        log.info("acknowledged %s", name)
        return t["acked"].format(name=f"<code>{esc(name)}</code>")
