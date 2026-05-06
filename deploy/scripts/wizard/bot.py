"""Configure the interactive Telegram bot block.

Distinct from the Telegram *notifier* (which only sends alerts/digest):
the bot in `bot/` package replies to commands (/start, /help, /status, ...)
and runs actions on behalf of admin users. This step prompts the operator
for whether to enable it, which token to use, and who counts as admin.

Config shape written to YAML (`bot:`):
    enabled: bool                    # gate; bot/main.py won't poll if false
    token: str                       # optional — falls back to notifier token
    admins: list[int]                # TG user IDs allowed to issue commands
"""
from __future__ import annotations

from rich.prompt import Confirm, Prompt

from .config_io import load_existing
from .i18n import t
from .ui import console, warn_line


def _load_existing_bot() -> dict | None:
    """Pull the previously-written `bot:` block so re-runs can offer
    'keep current'. Returns None when no bot config has been saved yet.
    """
    raw = load_existing()
    bot = raw.get("bot")
    if not isinstance(bot, dict) or not bot.get("enabled"):
        return None
    return bot


def _parse_admins(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            warn_line(f"skipping non-numeric admin id: {chunk!r}")
    return out


def configure_bot(*, notifier_token: str | None) -> dict | None:
    """Returns the `bot:` config block, or None when the operator skips.

    `notifier_token` is the bot token chosen for the Telegram *notifier*
    earlier in the wizard — used to offer 'reuse same bot' when present.
    """
    console.print(f"  [dim italic]{t('bot_intro')}[/dim italic]")

    existing = _load_existing_bot()
    if existing:
        admins = existing.get("admins") or []
        token_hint = " + own token" if existing.get("token") else ""
        console.print(
            f"  [dim italic]{t('have_bot', admins=admins, token=token_hint)}[/dim italic]"
        )
        if Confirm.ask(f"  {t('ask_keep_bot')}", default=True):
            return existing

    if not Confirm.ask(f"  {t('ask_bot_yn')}", default=False):
        return None

    # Token: same as notifier (default when one exists), else prompt for separate.
    token: str = ""
    if notifier_token and Confirm.ask(
        f"  {t('ask_bot_same_token')}", default=True,
    ):
        # Leave `token` empty in the YAML — bot/config.py falls back to the
        # first telegram notifier's bot_token, so they stay in sync.
        token = ""
    else:
        console.print(f"  [dim]{t('bot_separate_token_hint')}[/dim]")
        token = Prompt.ask(t("ask_bot_token"), password=True) or ""

    # Admins — operator's TG *user* id (not chat id). Empty list = bot
    # refuses every command until the env var or YAML is filled in later.
    admins_raw = Prompt.ask(t("ask_bot_admins"), default="")
    admins = _parse_admins(admins_raw)
    if not admins:
        warn_line(t("bot_admins_empty"))

    out: dict = {"enabled": True, "admins": admins}
    if token:
        out["token"] = token
    return out
