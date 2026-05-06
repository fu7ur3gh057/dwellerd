"""Wizard translation dictionary + language picker.

`LANG` is a module attribute updated by `set_lang` / `pick_language`. Other
modules call `t("key", **kwargs)` which reads it lazily — so the language
choice from the user is reflected everywhere without passing it around.
"""
from __future__ import annotations

import os

from rich.prompt import Prompt

from .ui import console


LANG = "en"


LOCALES: dict[str, dict[str, str]] = {
    "en": {
        "subtitle": "🦆  server monitoring → telegram",
        "lang_q": "Language / Язык",
        "menu_title": "Choose action",
        "menu_dev": "run in dev mode (foreground)",
        "menu_service": "install as systemd service",
        "menu_uninstall": "uninstall systemd service",
        "menu_edit": "edit config (re-run wizard)",
        "menu_exit": "exit",
        "config_found": "config found",
        "config_missing": "no config yet — we'll create one",

        # ─ sections ─
        "section_tg": "Telegram",
        "section_host": "Host",
        "section_disks": "Disks",
        "section_net": "Network",
        "section_systemd": "Systemd units",
        "section_docker": "Docker compose",
        "section_logs": "Logs (errors → digest)",
        "section_web": "Web client (FastAPI)",
        "section_writing": "Writing",
        "section_service": "Systemd service install",
        "section_uninstall": "Systemd service uninstall",

        # ─ telegram ─
        "ask_bot_token": "bot token",
        "ask_chat_id": "chat id",
        "ask_proxy_yn": "use SOCKS5 proxy for Telegram?",
        "ask_proxy_host": "  proxy host",
        "ask_proxy_port": "  proxy port",
        "ask_proxy_user": "  proxy user (empty if none)",
        "ask_proxy_pass": "  proxy password (empty if none)",
        "ask_skip_telegram": "skip Telegram for now? (you can add later)",
        "have_tg_creds": "found existing Telegram setup: chat_id={chat_id}{proxy}",
        "ask_keep_tg_creds": "keep current bot token and chat id?",
        "ask_keep_proxy": "keep current proxy ({proxy})?",
        "telegram_hint_chat": "tip: message your bot, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and look for chat.id",

        # ─ host ─
        "ask_hostname": "hostname",
        "ask_check_int": "check interval, sec — how often cpu/mem/disk/systemd run (min 30)",
        "ask_report_int": "report interval, sec — full status digest in TG (min 30)",
        "min_clamp": "value below {minimum}s — clamping to {minimum}s",
        "ask_warn": "warn threshold %",
        "ask_crit": "crit threshold %",

        # ─ disks ─
        "step_detect_disks": "detecting mounted disks",
        "disks_pick": "Which disks to monitor?",
        "disks_custom": "+ add path manually",
        "disks_custom_input": "additional paths (csv, empty = none)",
        "ask_disk_paths": "paths (comma-separated)",

        # ─ network ─
        "step_detect_net": "detecting network interfaces",
        "net_pick": "Which interfaces to track for traffic?",
        "net_none": "no interfaces detected, will sum all",

        # ─ systemd ─
        "ask_systemd_yn": "monitor systemd services?",
        "step_detect_systemd": "detecting running services",
        "systemd_pick": "Which services to alert on if they go down?",
        "systemd_none": "no user services detected",

        # ─ docker ─
        "ask_docker_yn": "monitor docker compose?",
        "ask_compose_path": "compose path (empty = done)",
        "ask_containers": "containers (csv, empty = all)",
        "ask_starred": "starred (csv, empty = none)",
        "step_detecting": "detecting running compose projects",
        "docker_none_found": "no running compose projects detected — falling back to manual entry",
        "docker_pick_projects": "Which compose projects to monitor?",
        "docker_custom_path": "+ add path manually",
        "docker_pick_containers": "Containers to watch in '{project}'",
        "docker_pick_starred": "Star ⭐ which? (optional)",
        "docker_hint": r"\[space] toggle  \[enter] confirm  \[a] toggle all",

        # ─ logs ─
        "ask_logs_yn": "collect logs and ship error digests?",
        "step_detect_logs": "probing for log sources",
        "logs_pick": "Which log sources to collect?",
        "logs_no_candidates": "no log sources detected on this host",
        "logs_journal_warn": "{user} is not in the `systemd-journal` group — without it the daemon can't read system journal in dev mode",
        "logs_journal_auto": "add automatically? (sudo usermod -aG systemd-journal {user})",
        "logs_journal_done": "group membership updated — for the current shell run `newgrp systemd-journal` or re-login ({user})",
        "logs_journal_failed": "automatic add failed",
        "logs_journal_manual": "run this in another terminal:",
        "logs_journal_recheck": "check again?",
        "logs_journal_still_no": "still not in the group — try `newgrp` or re-login",
        "ask_logs_digest": "  digest interval, sec — how often the dedup roll-up fires",
        "ask_logs_retention": "  retention, days — auto-prune events older than this",
        "ask_logs_max_rows": "  max rows — hard cap on the log_events table",
        "logs_label_journal": "journal · {unit}",
        "logs_label_file": "file · {path}",
        "logs_label_docker": "docker · {service}",
        "logs_hint_perm": "[dim]not readable for {user} — daemon must run as root or fix perms[/dim]",

        # ─ bot ─
        "section_bot": "Bot (interactive)",
        "bot_intro": "Interactive Telegram bot — replies to commands (/start, /help, /status, ...). NOT just a notifier — every admin you list can issue commands and trigger actions.",
        "ask_bot_yn": "enable the interactive bot?",
        "ask_bot_same_token": "use the same Telegram bot as the notifier? (one bot does both alerts and commands)",
        "ask_bot_token": "  separate bot token (different from the notifier one)",
        "ask_bot_admins": "  admin Telegram user IDs (comma-separated, find via @userinfobot)",
        "bot_admins_empty": "no admin ids — bot will refuse all commands until you add some via DWELLERD_BOT_ADMINS env or bot.admins in config.yaml",
        "bot_separate_token_hint": "tip: a dedicated bot for commands lets the notifier stay write-only — different perms, less risk",
        "have_bot": "found existing bot setup: admins={admins}{token}",
        "ask_keep_bot": "keep current bot token and admin list?",
        "bot_url_hint": "after start: open @{username} in Telegram and send /start (run `make run-bot` to start polling)",

        # ─ web ─
        "ask_web_yn": "expose the web client (Swagger + admin API)?",
        "ask_web_port": "  port",
        "ask_web_username": "  admin username",
        "ask_web_password": "  admin password (8+ chars)",
        "ask_web_password_confirm": "  repeat password",
        "ask_web_keep_user": "found existing admin user '{username}' — keep current password?",
        "web_password_short": "password too short, min 8 chars — try again",
        "web_password_mismatch": "passwords don't match — try again",
        "ask_terminal_yn": "enable in-browser TERMINAL? (root shell — opt-in, system user/password via PAM)",
        "ask_terminal_shell": "  shell — leave blank to use each user's /etc/passwd entry (recommended)",
        "ask_terminal_allow": "  allow_users (comma-separated, blank = any system user)",
        "ask_terminal_ttl": "  unlock-token TTL, sec — auto-relock after",
        "terminal_warn": "[yellow]![/yellow] every keystroke is logged to terminal_audit (incl. typed passwords); login uses normal SYSTEM accounts via PAM",
        "step_client_install": "installing client deps (npm)",
        "step_client_build":   "building client bundle (next build)",
        "step_restart_service": "restarting dwellerd.service",
        "client_pkg_missing": "node/npm not found — skip client build (install Node 20+ then run `make client-build`)",
        "client_build_skipped": "client/ folder missing — skip build",
        "client_build_failed": "client build failed — re-run `make client-build` after fixing the error above",
        "web_url_hint": "after start: http://<vps-ip>:{port}/dwellerd/api/docs",
        "web_summary_header": "Web client URLs (paste into browser):",
        "web_summary_swagger": "  Swagger UI:   {url}",
        "web_summary_redoc":   "  ReDoc:        {url}",
        "web_summary_openapi": "  OpenAPI JSON: {url}",
        "web_summary_health":  "  Healthcheck:  {url}",
        "web_summary_login":   "  login as:     {username}",
        "web_summary_firewall": "if it doesn't open — open port {port} in your VPS firewall (ufw allow {port}, or your provider's panel)",

        # ─ writing ─
        "step_backup": "backing up to {name}",
        "step_write": "writing config.yaml",
        "step_db_persist": "writing settings + admin to DB",
        "step_venv": "creating venv",
        "step_deps": "installing dependencies",

        # ─ service ─
        "step_unit": "writing systemd unit",
        "step_config": "copying config.yaml to {dst}",
        "step_reload": "systemctl daemon-reload",
        "step_enable": "systemctl enable --now",
        "step_disable": "systemctl disable --now",
        "step_remove_unit": "removing unit file",
        "step_purge_user": "userdel dwellerd",
        "step_purge_home": "rm -rf {path}",
        "step_chown_dev": "fixing ownership of data/ + logs/ for current user",
        "service_done": "service installed and started",
        "service_removed": "service stopped and removed",
        "no_service": "no systemd unit at {path} — nothing to remove",
        "confirm_install": "install systemd unit at {unit}?",
        "confirm_uninstall": "stop and remove the systemd unit?",
        "confirm_purge": "also remove user '{user}', {home} and {etc}? (DESTROYS COLLECTED DATA)",
        "preflight_running": "running preflight diagnostics for '{user}'",
        "logs_hint": "logs:    sudo journalctl -u dwellerd -f",
        "status_hint": "status:  systemctl status dwellerd",
        "user_lines": "User=dwellerd · service writes /var/lib/dwellerd, reads /etc/dwellerd",
        "control_panel_warn": "if `docker compose` projects live under /var/www/<panel-user>/ with restrictive ACLs (FastPanel/ISPmanager), the dwellerd user may not be able to read them — re-run with --as-root",
        "unit_user_root": "service user (--as-root): [bold red]root[/bold red] — fallback for control-panel hosts",
        "unit_user_normal": "service user: [bold cyan]dwellerd[/bold cyan] — separate system user, FHS layout",
        "venv_missing": "venv missing — run `make install` first",
        "user_missing": "system user '{user}' missing — run `make bootstrap-user` first",
        "starting_dev": "starting Dwellerd (Ctrl+C to stop)",
        "no_config": "config.yaml missing — run wizard first",
        "warn_not_found": "{path} not found, including anyway",
        "bye": "bye",
        "aborted": "aborted",
        "choice": "choice",
        "sudo_hint": "sudo: enter password if asked",
        "sudo_failed": "sudo failed",
        "db_skipped": "DB write skipped: server/ not importable from this venv",
        "purge_skipped": "purge skipped — user and data preserved",
    },
    "ru": {
        "subtitle": "🦆  мониторинг сервера → telegram",
        "lang_q": "Язык / Language",
        "menu_title": "Что делаем",
        "menu_dev": "запустить в dev-режиме (foreground)",
        "menu_service": "поставить как systemd-сервис",
        "menu_uninstall": "удалить systemd-сервис",
        "menu_edit": "переписать конфиг (мастер заново)",
        "menu_exit": "выход",
        "config_found": "найден конфиг",
        "config_missing": "конфига ещё нет — создадим",

        "section_tg": "Telegram",
        "section_host": "Хост",
        "section_disks": "Диски",
        "section_net": "Сеть",
        "section_systemd": "Systemd-сервисы",
        "section_docker": "Docker compose",
        "section_logs": "Логи (ошибки → дайджест)",
        "section_web": "Веб-клиент (FastAPI)",
        "section_writing": "Запись",
        "section_service": "Установка systemd-сервиса",
        "section_uninstall": "Удаление systemd-сервиса",

        "ask_bot_token": "токен бота",
        "ask_chat_id": "chat id",
        "ask_proxy_yn": "использовать SOCKS5-прокси для Telegram?",
        "ask_proxy_host": "  хост прокси",
        "ask_proxy_port": "  порт прокси",
        "ask_proxy_user": "  юзер прокси (пусто если без авторизации)",
        "ask_proxy_pass": "  пароль прокси (пусто если без авторизации)",
        "ask_skip_telegram": "пропустить Telegram пока? (потом добавишь)",
        "have_tg_creds": "найдены данные Telegram: chat_id={chat_id}{proxy}",
        "ask_keep_tg_creds": "оставить текущий токен и chat id?",
        "ask_keep_proxy": "оставить текущий прокси ({proxy})?",
        "telegram_hint_chat": "подсказка: напиши боту, потом `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — там chat.id",

        "ask_hostname": "hostname",
        "ask_check_int": "интервал проверок, сек — как часто запускать cpu/mem/disk/systemd (мин. 30)",
        "ask_report_int": "интервал репорта, сек — полный отчёт в TG (мин. 30)",
        "min_clamp": "значение меньше {minimum}с — округляю до {minimum}с",
        "ask_warn": "warn порог %",
        "ask_crit": "crit порог %",

        "step_detect_disks": "ищу примонтированные диски",
        "disks_pick": "Какие диски мониторим?",
        "disks_custom": "+ добавить путь вручную",
        "disks_custom_input": "доп. пути (csv, пусто = нет)",
        "ask_disk_paths": "пути (через запятую)",

        "step_detect_net": "ищу сетевые интерфейсы",
        "net_pick": "По каким интерфейсам считать трафик?",
        "net_none": "интерфейсов не найдено, буду считать все",

        "ask_systemd_yn": "мониторить systemd-сервисы?",
        "step_detect_systemd": "ищу запущенные сервисы",
        "systemd_pick": "Какие сервисы алертить при падении?",
        "systemd_none": "пользовательских сервисов не найдено",

        "ask_docker_yn": "мониторить docker compose?",
        "ask_compose_path": "путь к compose (пусто = готово)",
        "ask_containers": "контейнеры (csv, пусто = все)",
        "ask_starred": "избранные (csv, пусто = нет)",
        "step_detecting": "ищу запущенные compose-проекты",
        "docker_none_found": "запущенных compose-проектов не найдено — переходим к ручному вводу",
        "docker_pick_projects": "Какие compose-проекты мониторим?",
        "docker_custom_path": "+ добавить путь вручную",
        "docker_pick_containers": "Контейнеры для мониторинга в '{project}'",
        "docker_pick_starred": "Какие пометить ⭐? (опционально)",
        "docker_hint": r"\[пробел] выбор  \[enter] подтвердить  \[a] все",

        "ask_logs_yn": "собирать логи и слать дайджест ошибок?",
        "step_detect_logs": "ищу источники логов",
        "logs_pick": "Какие источники логов собираем?",
        "logs_no_candidates": "источников логов на этом хосте не нашлось",
        "logs_journal_warn": "{user} не в группе `systemd-journal` — без неё демон не сможет читать system journal в dev-режиме",
        "logs_journal_auto": "добавить автоматически? (sudo usermod -aG systemd-journal {user})",
        "logs_journal_done": "группа добавлена — для текущей сессии: `newgrp systemd-journal` либо перезайди ({user})",
        "logs_journal_failed": "автоматически не вышло",
        "logs_journal_manual": "выполни в другом терминале:",
        "logs_journal_recheck": "проверить?",
        "logs_journal_still_no": "всё ещё не в группе — попробуй `newgrp` или перезайди",
        "ask_logs_digest": "  интервал дайджеста, сек — как часто шлём роллап",
        "ask_logs_retention": "  retention, дней — автоматическое удаление старше",
        "ask_logs_max_rows": "  макс. строк — хард-кап таблицы log_events",
        "logs_label_journal": "journal · {unit}",
        "logs_label_file": "file · {path}",
        "logs_label_docker": "docker · {service}",
        "logs_hint_perm": "[dim]не читается для {user} — демон должен быть от root или поправь права[/dim]",

        # ─ bot ─
        "section_bot": "Бот (интерактивный)",
        "bot_intro": "Интерактивный Telegram-бот — отвечает на команды (/start, /help, /status, ...). Это НЕ просто отправка алертов — каждый админ из списка может давать команды и запускать действия.",
        "ask_bot_yn": "включить интерактивного бота?",
        "ask_bot_same_token": "использовать тот же бот, что и нотифаер? (один бот шлёт алерты и принимает команды)",
        "ask_bot_token": "  отдельный токен бота (другой, чем у нотифаера)",
        "ask_bot_admins": "  Telegram user ID админов (через запятую, узнать у @userinfobot)",
        "bot_admins_empty": "список админов пуст — бот будет отклонять все команды, пока не добавишь через DWELLERD_BOT_ADMINS env или bot.admins в config.yaml",
        "bot_separate_token_hint": "подсказка: отдельный бот для команд позволяет нотифаеру остаться write-only — разные права, меньше рисков",
        "have_bot": "найдены настройки бота: admins={admins}{token}",
        "ask_keep_bot": "оставить текущий токен и список админов?",
        "bot_url_hint": "после старта: открой @{username} в Telegram и пошли /start (запуск: `make run-bot`)",

        "ask_web_yn": "поднимать веб-клиент (Swagger + admin API)?",
        "ask_web_port": "  порт",
        "ask_web_username": "  логин админа",
        "ask_web_password": "  пароль админа (8+ символов)",
        "ask_web_password_confirm": "  повтори пароль",
        "ask_web_keep_user": "найден админ '{username}' — оставить текущий пароль?",
        "web_password_short": "слишком короткий пароль (минимум 8 символов) — попробуй ещё",
        "web_password_mismatch": "пароли не совпадают — попробуй ещё",
        "ask_terminal_yn": "включить ТЕРМИНАЛ в браузере? (root shell — opt-in, login через системные креды/PAM)",
        "ask_terminal_shell": "  shell — пусто = брать из /etc/passwd для каждого юзера (рекомендуется)",
        "ask_terminal_allow": "  allow_users (через запятую, пусто = любой системный юзер)",
        "ask_terminal_ttl": "  TTL токена анлока, сек — авто-блокировка после",
        "terminal_warn": "[yellow]![/yellow] каждое нажатие пишется в terminal_audit (включая набранные пароли); login — обычные СИСТЕМНЫЕ учётки через PAM",
        "step_client_install": "ставлю зависимости фронта (npm)",
        "step_client_build":   "собираю фронт (next build)",
        "step_restart_service": "рестартую dwellerd.service",
        "client_pkg_missing": "node/npm не найден — пропускаю сборку (поставь Node 20+ и запусти `make client-build`)",
        "client_build_skipped": "папка client/ не найдена — пропускаю сборку",
        "client_build_failed": "сборка фронта упала — после фикса ошибки выше запусти `make client-build`",
        "web_url_hint": "после старта: http://<ip-впс>:{port}/dwellerd/api/docs",
        "web_summary_header": "URL'ы веб-клиента (вставь в браузер):",
        "web_summary_swagger": "  Swagger UI:   {url}",
        "web_summary_redoc":   "  ReDoc:        {url}",
        "web_summary_openapi": "  OpenAPI JSON: {url}",
        "web_summary_health":  "  Healthcheck:  {url}",
        "web_summary_login":   "  логин:        {username}",
        "web_summary_firewall": "если не открывается — открой порт {port} в фаерволе VPS (ufw allow {port} или в панели хостера)",

        "step_backup": "бэкаплю в {name}",
        "step_write": "пишу config.yaml",
        "step_db_persist": "пишу settings + админа в БД",
        "step_venv": "создаю venv",
        "step_deps": "ставлю зависимости",

        "step_unit": "пишу systemd-юнит",
        "step_config": "копирую config.yaml в {dst}",
        "step_reload": "systemctl daemon-reload",
        "step_enable": "systemctl enable --now",
        "step_disable": "systemctl disable --now",
        "step_remove_unit": "удаляю unit-файл",
        "step_purge_user": "userdel dwellerd",
        "step_purge_home": "rm -rf {path}",
        "step_chown_dev": "возвращаю владельца data/ и logs/ текущему юзеру",
        "service_done": "сервис установлен и запущен",
        "service_removed": "сервис остановлен и удалён",
        "no_service": "юнита {path} нет — нечего удалять",
        "confirm_install": "поставить systemd-юнит в {unit}?",
        "confirm_uninstall": "остановить и удалить systemd-юнит?",
        "confirm_purge": "также удалить юзера '{user}', {home} и {etc}? (УДАЛИТ СОБРАННЫЕ ДАННЫЕ)",
        "preflight_running": "запускаю preflight-диагностику для '{user}'",
        "logs_hint": "логи:    sudo journalctl -u dwellerd -f",
        "status_hint": "статус:  systemctl status dwellerd",
        "user_lines": "User=dwellerd · сервис пишет в /var/lib/dwellerd, читает /etc/dwellerd",
        "control_panel_warn": "если `docker compose` проекты лежат под /var/www/<panel-user>/ с ACL (FastPanel/ISPmanager), dwellerd может не прочитать их — перезапусти с --as-root",
        "unit_user_root": "пользователь сервиса (--as-root): [bold red]root[/bold red] — fallback для control-panel хостов",
        "unit_user_normal": "пользователь сервиса: [bold cyan]dwellerd[/bold cyan] — отдельный системный юзер, FHS-разметка",
        "venv_missing": "venv не найден — сначала `make install`",
        "user_missing": "системный юзер '{user}' не найден — сначала `make bootstrap-user`",
        "starting_dev": "запускаю Dwellerd (Ctrl+C чтобы остановить)",
        "no_config": "config.yaml не найден — сначала запусти мастер",
        "warn_not_found": "{path} не найден, добавляю всё равно",
        "bye": "пока",
        "aborted": "прервано",
        "choice": "выбор",
        "sudo_hint": "sudo: введи пароль если попросит",
        "sudo_failed": "sudo не отработал",
        "db_skipped": "запись в БД пропущена: server/ не импортируется из этого venv",
        "purge_skipped": "purge пропущен — юзер и данные сохранены",
    },
}


def t(key: str, **kwargs) -> str:
    """Lookup with fallback to en, then to the raw key. Format with kwargs."""
    s = LOCALES.get(LANG, LOCALES["en"]).get(key) or LOCALES["en"].get(key) or key
    if kwargs:
        try:
            return s.format(**kwargs)
        except (KeyError, IndexError):
            return s
    return s


def set_lang(lang: str) -> None:
    global LANG
    if lang in LOCALES:
        LANG = lang


def detect_lang() -> str:
    """Default to RU only if the locale clearly says so. Anything else → EN."""
    env = (os.environ.get("DWELLERD_LANG")
           or os.environ.get("LC_ALL")
           or os.environ.get("LANG", "")).lower()
    return "ru" if env.startswith("ru") else "en"


def pick_language() -> None:
    """Two-key prompt — 1 for English, 2 for Russian. Default follows locale."""
    default = detect_lang()
    console.print(
        "  [bold cyan]1[/bold cyan])[bold] English[/bold]   "
        "[bold cyan]2[/bold cyan])[bold] Русский[/bold]"
    )
    choice = Prompt.ask(
        f"[bold]{LOCALES[default]['lang_q']}[/bold]",
        choices=["1", "2"],
        default="1" if default == "en" else "2",
        show_choices=False,
    )
    set_lang("en" if choice == "1" else "ru")
