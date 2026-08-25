"""Wizard translations. `set_lang` once, `t(key)` everywhere after."""
from __future__ import annotations

_LANG = "en"

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "subtitle": "server monitoring for one host, straight to Telegram",
        "ask_lang": "Language / Язык [1] English  [2] Русский",

        "sec_tg": "Telegram",
        "tg_hint": "Create a bot with @BotFather, then send it any message and "
                   "get your chat id from @userinfobot.",
        "ask_token": "Bot token",
        "ask_chat": "Chat id (or -100... for a group)",
        "ask_proxy": "Proxy URL for Telegram (blank = none)",
        "tg_testing": "sending a test message",
        "tg_ok": "test message delivered",
        "tg_fail": "could not deliver the test message",
        "tg_retry": "Re-enter the token and chat id?",
        "tg_skip": "Telegram left unconfigured — alerts will only go to the journal",

        "sec_host": "Host",
        "ask_hostname": "Name for this server (shown in every message)",
        "ask_interval": "How often to run the checks, seconds",
        "ask_warn": "Warning threshold, %",
        "ask_crit": "Critical threshold, %",

        "sec_disks": "Disks",
        "disks_found": "Mount points found:",
        "ask_disks": "Which to watch (numbers, or 'all')",

        "sec_services": "Services",
        "ask_systemd_yn": "Watch systemd units?",
        "ask_systemd": "Which units (numbers, or 'all')",
        "ask_http_yn": "Watch HTTP endpoints?",
        "ask_http_url": "URL (blank to stop adding)",
        "ask_http_status": "Expected status code",

        "sec_docker": "Docker",
        "docker_missing": "docker not found — skipping this section",
        "ask_docker_yn": "Watch docker containers?",
        "containers_found": "Containers found:",
        "ask_containers": "Which to watch (numbers, 'all', or blank for "
                          "everything docker reports)",

        "sec_logs": "Logs",
        "ask_logs_yn": "Collect logs and report new errors?",
        "ask_log_level": "Which lines count as errors [1] error  [2] warn  "
                         "[3] info  [4] everything",
        "ask_log_containers": "Read logs from which containers (numbers, or 'all')",
        "ask_log_files": "Log file path (blank to stop adding)",
        "ask_log_pattern": "Regex to match (blank = the level filter only)",
        "ask_digest": "Digest interval, seconds",
        "ask_retention": "Keep log history for, days",

        "sec_report": "Periodic report",
        "ask_report_yn": "Send a periodic status report?",
        "ask_report_int": "Report interval, seconds",

        "sec_write": "Writing the config",
        "written": "config written to",
        "backed_up": "previous config backed up to",
        "ask_overwrite": "A config already exists at {path}. Overwrite?",
        "aborted": "Aborted — nothing was written.",

        "next_steps": "Next steps",
        "next_dev": "Run it in the foreground:  make run",
        "next_install": "Install as a systemd service:  sudo make install",
        "done": "Setup complete.",
    },
    "ru": {
        "subtitle": "мониторинг одного сервера — сразу в Telegram",
        "ask_lang": "Language / Язык [1] English  [2] Русский",

        "sec_tg": "Telegram",
        "tg_hint": "Создай бота через @BotFather, напиши ему любое сообщение "
                   "и возьми свой chat id у @userinfobot.",
        "ask_token": "Токен бота",
        "ask_chat": "Chat id (или -100... для группы)",
        "ask_proxy": "Proxy для Telegram (пусто = без прокси)",
        "tg_testing": "отправляю тестовое сообщение",
        "tg_ok": "тестовое сообщение доставлено",
        "tg_fail": "не удалось доставить тестовое сообщение",
        "tg_retry": "Ввести токен и chat id заново?",
        "tg_skip": "Telegram не настроен — алерты будут только в журнале",

        "sec_host": "Хост",
        "ask_hostname": "Имя сервера (будет в каждом сообщении)",
        "ask_interval": "Как часто запускать проверки, секунд",
        "ask_warn": "Порог warning, %",
        "ask_crit": "Порог critical, %",

        "sec_disks": "Диски",
        "disks_found": "Найденные точки монтирования:",
        "ask_disks": "За какими следить (номера или 'all')",

        "sec_services": "Сервисы",
        "ask_systemd_yn": "Следить за systemd-юнитами?",
        "ask_systemd": "За какими (номера или 'all')",
        "ask_http_yn": "Следить за HTTP-эндпоинтами?",
        "ask_http_url": "URL (пусто — закончить)",
        "ask_http_status": "Ожидаемый статус",

        "sec_docker": "Docker",
        "docker_missing": "docker не найден — пропускаю раздел",
        "ask_docker_yn": "Следить за docker-контейнерами?",
        "containers_found": "Найденные контейнеры:",
        "ask_containers": "За какими следить (номера, 'all', или пусто — "
                          "за всеми, что покажет docker)",

        "sec_logs": "Логи",
        "ask_logs_yn": "Собирать логи и сообщать о новых ошибках?",
        "ask_log_level": "Что считать ошибкой [1] error  [2] warn  "
                         "[3] info  [4] всё подряд",
        "ask_log_containers": "С каких контейнеров читать логи (номера или 'all')",
        "ask_log_files": "Путь к лог-файлу (пусто — закончить)",
        "ask_log_pattern": "Regex для фильтра (пусто = только фильтр уровня)",
        "ask_digest": "Интервал дайджеста, секунд",
        "ask_retention": "Хранить историю логов, дней",

        "sec_report": "Периодический отчёт",
        "ask_report_yn": "Присылать периодический отчёт о состоянии?",
        "ask_report_int": "Интервал отчёта, секунд",

        "sec_write": "Запись конфига",
        "written": "конфиг записан в",
        "backed_up": "предыдущий конфиг сохранён как",
        "ask_overwrite": "Конфиг уже есть: {path}. Перезаписать?",
        "aborted": "Отменено — ничего не записано.",

        "next_steps": "Дальше",
        "next_dev": "Запустить в терминале:  make run",
        "next_install": "Поставить как systemd-сервис:  sudo make install",
        "done": "Настройка завершена.",
    },
}


def set_lang(lang: str) -> None:
    global _LANG
    _LANG = "ru" if str(lang).lower().startswith("ru") else "en"


def lang() -> str:
    return _LANG


def t(key: str, **kwargs) -> str:
    text = _STRINGS.get(_LANG, _STRINGS["en"]).get(key) or _STRINGS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text
