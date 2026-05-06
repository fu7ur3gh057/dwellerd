"""Reset (or set) a user's password in the dwellerd `users` table.

Use when:
  - You forgot the password set during wizard
  - You want to set a fresh password for an existing admin
  - You need to create a missing user from the CLI

    PYTHONPATH=server .venv/bin/python deploy/scripts/reset-password.py admin

The script prompts twice for the new password (with confirmation), hashes
via the same `web.auth.passwords.hash_password` the wizard uses, and
upserts the row. No daemon restart needed — `users` is read fresh at
each /login.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_db_path() -> Path:
    for p in (Path("/etc/dwellerd/config.yaml"), PROJECT_ROOT / "config.yaml"):
        if p.exists():
            try:
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            if (db := (cfg.get("db") or {}).get("path")):
                return Path(db)
    return PROJECT_ROOT / "data" / "dwellerd.sqlite"


def main() -> int:
    p = argparse.ArgumentParser(prog="dwellerd-reset-password")
    p.add_argument("username", help="user whose password to set")
    p.add_argument("--role", default="admin",
                   help="role for newly-created users (default: admin)")
    p.add_argument("--create-if-missing", action="store_true",
                   help="create the user row if it doesn't exist yet")
    args = p.parse_args()

    sys.path.insert(0, str(PROJECT_ROOT / "server"))
    try:
        from sqlmodel import Session, select  # type: ignore
        from sqlalchemy import create_engine
        from db.models import User  # type: ignore
        from web.auth.passwords import hash_password  # type: ignore
    except ImportError as e:
        print(f"❌ failed to import server modules: {e}", file=sys.stderr)
        print("   make sure venv is set up: make install", file=sys.stderr)
        return 1

    db_path = _resolve_db_path()
    if not db_path.exists():
        print(f"❌ DB not found: {db_path}", file=sys.stderr)
        return 1

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == args.username)).first()
        if user is None and not args.create_if_missing:
            print(
                f"❌ user '{args.username}' not found.\n"
                f"   pass --create-if-missing to create them with role={args.role}",
                file=sys.stderr,
            )
            return 1

        # Two-step password prompt with confirmation. getpass hides input.
        while True:
            pwd = getpass.getpass(f"new password for '{args.username}' (8+ chars): ")
            if len(pwd) < 8:
                print("  пароль слишком короткий — минимум 8 символов")
                continue
            confirm = getpass.getpass("повтори пароль: ")
            if pwd != confirm:
                print("  пароли не совпадают")
                continue
            break

        try:
            hashed = hash_password(pwd)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

        if user is None:
            s.add(User(
                username=args.username,
                password_hash=hashed,
                role=args.role,
                is_active=True,
                created_at=time.time(),
            ))
            print(f"✅ created user '{args.username}' (role={args.role})")
        else:
            user.password_hash = hashed
            user.is_active = True
            s.add(user)
            print(f"✅ password reset for '{args.username}' (id={user.id}, role={user.role})")
        s.commit()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
