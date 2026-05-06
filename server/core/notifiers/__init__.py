from config import NotifierConfig
from .base import Alert, Notifier
from .telegram import TelegramNotifier

__all__ = ["Alert", "Notifier", "build_notifier"]


def build_notifier(cfg: NotifierConfig) -> Notifier:
    if cfg.type == "telegram":
        return TelegramNotifier(**cfg.options)
    raise ValueError(f"unknown notifier type: {cfg.type}")
