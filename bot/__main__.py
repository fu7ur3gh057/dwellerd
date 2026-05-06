"""`python -m bot` entry — defers to bot.main.run() so handlers can also be
imported as a library (e.g. unit tests) without spinning up polling.
"""
from __future__ import annotations

import asyncio

from .main import run


if __name__ == "__main__":
    asyncio.run(run())
