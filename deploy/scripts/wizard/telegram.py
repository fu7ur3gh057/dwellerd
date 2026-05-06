"""Gather Telegram credentials (bot token, chat id, optional SOCKS5 proxy).

Re-uses values from an existing config.yaml when present so the operator
isn't forced to retype the bot token on every wizard re-run.
"""
from __future__ import annotations

import re

from rich.prompt import Confirm, Prompt

from .config_io import load_existing_telegram
from .i18n import t
from .ui import console


def _mask_proxy(url: str) -> str:
    """`socks5h://user:pass@host:port` → `socks5h://user:***@host:port`."""
    return re.sub(r"(://[^:@/]+):[^@]*(@)", r"\1:***\2", url) or url


def _ask_proxy() -> str:
    """Build a SOCKS5 URL from interactive prompts. Returns empty string if
    the user enters no host. Auth fields are skipped when the user is empty
    — Telegram-via-Tor and similar setups rarely need creds."""
    p_host = Prompt.ask(t("ask_proxy_host"))
    if not p_host:
        return ""
    p_port = Prompt.ask(t("ask_proxy_port"), default="1080")
    p_user = Prompt.ask(t("ask_proxy_user"), default="", show_default=False)
    p_pass = ""
    if p_user:
        p_pass = Prompt.ask(
            t("ask_proxy_pass"), default="", show_default=False, password=True,
        )
    if p_user:
        return f"socks5h://{p_user}:{p_pass}@{p_host}:{p_port}"
    return f"socks5h://{p_host}:{p_port}"


def gather_telegram() -> tuple[str, str, str] | None:
    """Returns (bot_token, chat_id, proxy_url) — proxy may be ''. Returns
    None when the operator opts to skip the Telegram block entirely.

    On a re-run we offer to reuse the existing creds; if not, we prompt
    fresh and optionally swap the proxy.
    """
    if Confirm.ask(f"  {t('ask_skip_telegram')}", default=False):
        return None

    existing = load_existing_telegram()
    if existing:
        proxy_existing = existing.get("proxy") or ""
        proxy_hint = " + proxy" if proxy_existing else ""
        console.print(
            f"  [dim italic]{t('have_tg_creds', chat_id=existing['chat_id'], proxy=proxy_hint)}[/dim italic]"
        )
        if Confirm.ask(f"  {t('ask_keep_tg_creds')}", default=True):
            bot_token = existing["bot_token"]
            chat_id = str(existing["chat_id"])
            if proxy_existing:
                masked = _mask_proxy(proxy_existing)
                if Confirm.ask(f"  {t('ask_keep_proxy', proxy=masked)}", default=True):
                    return bot_token, chat_id, proxy_existing
                if Confirm.ask(f"  {t('ask_proxy_yn')}", default=False):
                    return bot_token, chat_id, _ask_proxy()
                return bot_token, chat_id, ""
            if Confirm.ask(f"  {t('ask_proxy_yn')}", default=False):
                return bot_token, chat_id, _ask_proxy()
            return bot_token, chat_id, ""

    bot_token = Prompt.ask(f"  [bold]{t('ask_bot_token')}[/bold]", password=True)
    if not bot_token:
        return None
    console.print(f"    [dim]{t('telegram_hint_chat')}[/dim]")
    chat_id = Prompt.ask(f"  [bold]{t('ask_chat_id')}[/bold]")
    if not chat_id:
        return None
    proxy_url = ""
    if Confirm.ask(f"  {t('ask_proxy_yn')}", default=False):
        proxy_url = _ask_proxy()
    return bot_token, chat_id, proxy_url
