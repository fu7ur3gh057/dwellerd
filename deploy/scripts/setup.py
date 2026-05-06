"""Dwellerd setup wizard — argparse + main loop only.

Everything substantive lives in `wizard/` (one-purpose-per-file modules);
this file just parses CLI args, picks the language, and dispatches to:

    --install-service       → wizard.install_systemd
    --uninstall-service     → wizard.uninstall_systemd
    (no flag, interactive)  → menu loop (run_wizard / install / uninstall / dev)

Run as:
    deploy/scripts/setup.sh                 # interactive
    deploy/scripts/setup.py --install-service [--as-root]
    deploy/scripts/setup.py --uninstall-service [--purge]

Flags:
    --lang ru|en            force UI language (default: $LANG / $LC_ALL)
    --as-root               with --install-service: run unit as root
                            (FastPanel/ISPmanager fallback). Default
                            uses the dedicated `dwellerd` system user.
    --purge                 with --uninstall-service: also drop the
                            user, /var/lib/dwellerd and /etc/dwellerd
"""
from __future__ import annotations

import argparse
import sys

from wizard import (
    banner,
    banner_minimal,
    console,
    detect_lang,
    install_systemd,
    main_menu,
    pick_language,
    run_dev,
    run_wizard,
    set_lang,
    uninstall_systemd,
)
from wizard.i18n import t
from wizard.paths import DEV_CONFIG, PROD_CONFIG


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="dwellerd-setup")
    p.add_argument("--install-service", action="store_true",
                   help="skip menus, install systemd service directly")
    p.add_argument("--uninstall-service", action="store_true",
                   help="stop, disable and remove systemd service")
    p.add_argument("--purge", action="store_true",
                   help="with --uninstall-service: also remove user 'dwellerd', "
                        "/var/lib/dwellerd and /etc/dwellerd")
    p.add_argument("--as-root", action="store_true",
                   help="run the daemon as root (control-panel hosts: "
                        "FastPanel/ISPmanager); default is User=dwellerd")
    p.add_argument("--lang", choices=["en", "ru"], help="force ui language")
    return p.parse_args()


def main() -> int:
    args = _parse()

    # Language handling: explicit --lang wins; non-interactive flag flows
    # detect from $LANG; the interactive menu prompts the operator.
    if args.lang:
        set_lang(args.lang)
        banner(subtitle=t("subtitle"))
    elif args.install_service or args.uninstall_service:
        set_lang(detect_lang())
        banner(subtitle=t("subtitle"))
    else:
        try:
            console.clear()
        except Exception:
            pass  # tty without ANSI clear support — skip silently
        banner_minimal()
        pick_language()
        try:
            console.clear()
        except Exception:
            pass
        banner(subtitle=t("subtitle"))

    if args.uninstall_service:
        return uninstall_systemd(purge=args.purge)

    if args.install_service:
        # Make sure there's a config to copy into /etc/dwellerd/. Otherwise
        # the install would silently use the example, which has no notifiers
        # / checks → daemon starts but does nothing.
        if not DEV_CONFIG.exists() and not PROD_CONFIG.exists():
            run_wizard()
        return install_systemd(as_root=args.as_root)

    while True:
        action = main_menu()
        if action == "exit":
            console.print(f"[dim]{t('bye')}[/dim]")
            return 0
        if action == "edit":
            run_wizard()
            try:
                console.clear()
            except Exception:
                pass
            banner(subtitle=t("subtitle"))
            continue
        if action == "dev":
            if not DEV_CONFIG.exists():
                run_wizard()
            return run_dev()
        if action == "service":
            if not DEV_CONFIG.exists() and not PROD_CONFIG.exists():
                run_wizard()
            return install_systemd(as_root=args.as_root)
        if action == "uninstall":
            return uninstall_systemd(purge=args.purge)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print(f"\n[dim]{t('aborted')}[/dim]")
        sys.exit(130)
