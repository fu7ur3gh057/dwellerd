"""Dwellerd Telegram bot — aiogram 3.x.

Entry point: `python -m bot` (see __main__.py) or `make run-bot`.

Layout:
    main.py           — Bot + Dispatcher wiring, runs polling
    config.py         — load token + admin ids from env / config.yaml
    handlers/         — message + callback routers (one router per file)
    keyboards/        — InlineKeyboardMarkup / ReplyKeyboardMarkup builders
    middlewares/      — auth, throttle, logging
    states/           — aiogram.fsm StatesGroup definitions
    utils/            — small helpers (text formatting, parsing, etc.)
"""
