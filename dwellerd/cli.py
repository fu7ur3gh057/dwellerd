"""Command line: `dwellerd <command>`.

    setup     interactive wizard, writes the config
    run       run the daemon in the foreground (what systemd invokes)
    check     run every check once, print the results, exit
    test      send a test message to the configured chat
    report    build the periodic report now and send it
    config    print the resolved config path and a summary (--validate to lint it)
    mute      silence alerts for a while:  dwellerd mute 2h [pattern]
    unmute    lift one mute, or all of them
    mutes     list the active mutes
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


def _raw_problems(path: str | None):
    """Validation problems for the config file, or None if it cannot even
    be read (the caller has already reported that)."""
    from .config import read_raw
    from .validate import validate
    try:
        raw, _ = read_raw(Path(path) if path else None)
    except Exception:
        return None
    return validate(raw)


def _log_config_problems(cfg) -> None:
    """At startup, surface config problems in the journal without refusing
    to start: the daemon has always tolerated a bad entry by skipping it."""
    problems = _raw_problems(str(cfg.source_path) if cfg.source_path else None)
    log = logging.getLogger("dwellerd")
    for problem in problems or []:
        (log.error if problem.level == "error" else log.warning)(
            "config: %s", problem,
        )


def _open_storage(args):
    from .storage import Storage
    cfg = _load(args.config)
    storage = Storage(cfg.db)
    try:
        storage.connect()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
    return cfg, storage


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
    _log_config_problems(cfg)
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

    if args.validate:
        return _cmd_validate(args)

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
    a = cfg.alerts
    quiet = "off"
    if a.quiet is not None:
        quiet = (f"{a.quiet.start // 60:02d}:{a.quiet.start % 60:02d}"
                 f"-{a.quiet.end // 60:02d}:{a.quiet.end % 60:02d}")
    print(f"alerts:    confirm {a.confirm}x, hysteresis {a.hysteresis:g}%,"
          f" remind crit every {a.remind_after:.0f}s (0 = off),"
          f" group {a.group_window:g}s, quiet hours {quiet}")
    return 0


def _cmd_validate(args) -> int:
    """Lint the config file. Exit 0 = fine (warnings allowed), 2 = errors."""
    from .config import config_path, parse, read_raw
    from .validate import errors, validate

    target = Path(args.config) if args.config else config_path()
    try:
        raw, path = read_raw(target)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: cannot read the config: {e}", file=sys.stderr)
        return 2

    problems = validate(raw)
    for problem in problems:
        print(f"  {problem}")
    bad = errors(problems)
    if not bad:
        # Everything the validator knows about passed; make sure the real
        # parser agrees before declaring the file good.
        try:
            parse(raw, source=path)
        except Exception as e:
            print(f"  error: {type(e).__name__}: {e}")
            bad = [e]
    warnings = len(problems) - len(errors(problems))
    if bad:
        print(f"{path}: INVALID — {len(bad)} error(s), {warnings} warning(s)")
        return 2
    print(f"{path}: OK" + (f" — {warnings} warning(s)" if warnings else ""))
    return 0


def cmd_mute(args) -> int:
    import time
    from .duration import parse_duration

    try:
        seconds = parse_duration(args.duration)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    pattern = args.pattern or "*"
    cfg, storage = _open_storage(args)
    try:
        until = time.time() + seconds
        storage.add_mute(pattern, until)
    finally:
        storage.close()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
    print(f"muted {pattern!r} until {when}")
    if pattern == "*":
        print("(every check alert and every new-error alert; the digest and "
              "the periodic report still arrive)")
    return 0


def cmd_unmute(args) -> int:
    cfg, storage = _open_storage(args)
    try:
        n = storage.remove_mute(args.pattern)
    finally:
        storage.close()
    what = repr(args.pattern) if args.pattern else "all mutes"
    print(f"removed {what}" if n else f"nothing to remove for {what}")
    return 0


def cmd_mutes(args) -> int:
    import time

    cfg, storage = _open_storage(args)
    try:
        mutes = storage.active_mutes()
    finally:
        storage.close()
    if not mutes:
        print("no active mutes")
        return 0
    now = time.time()
    from .i18n import fmt_duration
    for m in mutes:
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["until"]))
        print(f"  {m['pattern']:<24} until {until}  ({fmt_duration(m['until'] - now)} left)")
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

    config = sub.add_parser("config", help="show the resolved configuration")
    config.add_argument("--validate", action="store_true",
                        help="check the config file for mistakes and exit")

    mute = sub.add_parser(
        "mute", help="silence alerts for a while (e.g. `mute 2h`, `mute 30m 'docker:*'`)",
    )
    mute.add_argument("duration", help="30m, 2h, 1d, 1h30m")
    mute.add_argument("pattern", nargs="?", default="*",
                      help="alert name or glob: cpu, 'disk-*', 'docker:*', "
                           "'log:*' (default: everything)")

    unmute = sub.add_parser("unmute", help="lift a mute (all of them if no pattern)")
    unmute.add_argument("pattern", nargs="?", default=None)

    sub.add_parser("mutes", help="list active mutes")
    return parser


_COMMANDS = {
    "setup": cmd_setup, "run": cmd_run, "check": cmd_check,
    "test": cmd_test, "report": cmd_report, "config": cmd_config,
    "mute": cmd_mute, "unmute": cmd_unmute, "mutes": cmd_mutes,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        raise SystemExit(0)
    _setup_logging(args.verbose)
    raise SystemExit(_COMMANDS[args.command](args))
