"""Command line: `dwellerd <command>`.

    setup     interactive wizard, writes the config
    run       run the daemon in the foreground (what systemd invokes)
    check     run every check once, print the results, exit
    test      send a test message to the configured chat
    report    build the periodic report now and send it
    config    print the resolved config path and a summary
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__


def _setup_logging(verbose: bool = False) -> None:
    # Under systemd, stdout goes to the journal, which adds its own
    # timestamps — so only print our own when running in a terminal.
    in_journal = bool(os.environ.get("JOURNAL_STREAM"))
    fmt = "%(levelname)s %(name)s: %(message)s" if in_journal else \
          "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load(path: str | None):
    from .config import load
    try:
        return load(Path(path) if path else None)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as e:
        print(f"error: cannot read the config: {e}", file=sys.stderr)
        raise SystemExit(2)


# ── commands ─────────────────────────────────────────────────────────────


def cmd_setup(args) -> int:
    from .config import DEV_CONFIG, SYSTEM_CONFIG
    from .wizard import run_wizard

    if args.config:
        target = Path(args.config)
    elif SYSTEM_CONFIG.parent.exists() and os.access(SYSTEM_CONFIG.parent, os.W_OK):
        target = SYSTEM_CONFIG
    else:
        target = DEV_CONFIG
    return 0 if run_wizard(target) else 1


def cmd_run(args) -> int:
    from .daemon import run

    cfg = _load(args.config)
    logging.getLogger("dwellerd").info(
        "dwellerd %s starting — config %s, db %s",
        __version__, cfg.source_path, cfg.db,
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_check(args) -> int:
    """One pass over every check, printed as a table. The fast way to see
    whether the thresholds and paths in the config make sense."""
    from .checks import build_checks

    cfg = _load(args.config)
    checks = build_checks(cfg)
    if not checks:
        print("no checks configured")
        return 1

    async def run_all() -> int:
        icons = {"ok": "✓", "warn": "!", "crit": "✗"}
        worst = "ok"
        rows: list[tuple[str, str, str]] = []
        for check in checks:
            try:
                if hasattr(check, "run_multi"):
                    results = await check.run_multi()
                else:
                    results = [(check.name, await check.run())]
            except Exception as e:
                rows.append((check.name, "crit", f"{type(e).__name__}: {e}"))
                worst = "crit"
                continue
            for name, result in results:
                rows.append((name, result.level, result.detail))
                if result.level == "crit" or (result.level == "warn" and worst == "ok"):
                    worst = result.level
        width = max(len(name) for name, _, _ in rows)
        for name, level, detail in rows:
            print(f"  {icons.get(level, '?')} {name.ljust(width)}  {detail}")
        return {"ok": 0, "warn": 1, "crit": 2}[worst]

    return asyncio.run(run_all())


def cmd_test(args) -> int:
    from .notify import Alert, TelegramNotifier

    cfg = _load(args.config)
    if not cfg.telegram.configured:
        print("error: telegram is not configured — run `dwellerd setup`",
              file=sys.stderr)
        return 2
    notifier = TelegramNotifier(
        cfg.telegram.bot_token, cfg.telegram.chat_id,
        lang=cfg.telegram.lang, proxy=cfg.telegram.proxy, hostname=cfg.hostname,
    )
    level = args.level if args.level in ("ok", "warn", "crit") else "warn"
    alert = Alert(
        check="memory", level=level, kind="memory",
        detail=f"RAM 87.4% >= 80%",
        metrics={"value": 87.4, "threshold": 80.0},
    )
    asyncio.run(notifier.send_alert(alert))
    print(f"sent a {level} test alert to chat {cfg.telegram.chat_id}")
    return 0


def cmd_report(args) -> int:
    from .notify import TelegramNotifier
    from .report import ReportBuilder
    from .storage import Storage

    cfg = _load(args.config)
    storage = Storage(cfg.db)
    storage.connect()

    async def build() -> int:
        text = await ReportBuilder(cfg, storage).build()
        if args.print or not cfg.telegram.configured:
            # Strip the HTML so a terminal reader sees the same content.
            import re
            print(re.sub(r"<[^>]+>", "", text))
            return 0
        notifier = TelegramNotifier(
            cfg.telegram.bot_token, cfg.telegram.chat_id,
            lang=cfg.telegram.lang, proxy=cfg.telegram.proxy,
            hostname=cfg.hostname,
        )
        await notifier.send_text(text)
        print(f"report sent to chat {cfg.telegram.chat_id}")
        return 0

    try:
        return asyncio.run(build())
    finally:
        storage.close()


def cmd_config(args) -> int:
    from .config import config_path

    cfg = _load(args.config)
    checks = cfg.checks
    print(f"config:    {cfg.source_path or config_path()}")
    print(f"database:  {cfg.db}")
    print(f"hostname:  {cfg.hostname or '(unset)'}")
    print(f"telegram:  {'configured' if cfg.telegram.configured else 'NOT configured'}"
          f" (lang {cfg.telegram.lang})")
    print(f"interval:  {checks.interval:.0f}s")
    print(f"disks:     {', '.join(d.path for d in checks.disks)}")
    print(f"http:      {len(checks.http)} probe(s)")
    print(f"systemd:   {len(checks.systemd)} unit(s)")
    print(f"docker:    {'on' if checks.docker.enabled else 'off'}"
          f" ({len(checks.docker.containers) or 'all'} container(s))")
    print(f"logs:      {'on' if cfg.logs.enabled else 'off'}"
          f" — {len(cfg.logs.sources)} source(s), level {cfg.logs.level}")
    print(f"report:    {'on' if cfg.report.enabled else 'off'}"
          f" every {cfg.report.interval:.0f}s")
    return 0


# ── entry point ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dwellerd",
        description="A small systemd daemon that watches a Linux host "
                    "and reports to Telegram.",
    )
    parser.add_argument("--version", action="version", version=f"dwellerd {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="interactive setup wizard")
    sub.add_parser("run", help="run the daemon in the foreground")
    sub.add_parser("check", help="run every check once and print the results")

    test = sub.add_parser("test", help="send a test alert to Telegram")
    test.add_argument("--level", default="warn", choices=["ok", "warn", "crit"])

    report = sub.add_parser("report", help="build and send the periodic report now")
    report.add_argument("--print", action="store_true",
                        help="print to stdout instead of sending")

    sub.add_parser("config", help="show the resolved configuration")
    return parser


_COMMANDS = {
    "setup": cmd_setup, "run": cmd_run, "check": cmd_check,
    "test": cmd_test, "report": cmd_report, "config": cmd_config,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        raise SystemExit(0)
    _setup_logging(args.verbose)
    raise SystemExit(_COMMANDS[args.command](args))
