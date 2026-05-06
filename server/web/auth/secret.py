"""JWT secret resolution.

Priority order, first match wins:
  1. `config.web.jwt.secret` — explicit operator override in config.yaml
  2. `DWELLERD_JWT_SECRET` environment variable — useful in containers
  3. `<data_dir>/jwt.secret` — auto-generated on first run, file mode 0600,
     persists across daemon restarts so issued tokens survive a reboot

The persistent file path is the meaningful improvement over Blackbox's
"regenerate on every restart" fallback: a misconfigured deploy that forgot
to set `web.jwt.secret` no longer kicks every active session out of the UI
when systemd restarts the unit.

The file is created with mode 0600. In production it lives at
`/var/lib/dwellerd/data/jwt.secret`, owned by the `dwellerd` system user —
nobody else can read it. The config.yaml stays clean of long-lived secrets.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from config import Config

log = logging.getLogger(__name__)

_SECRET_FILENAME = "jwt.secret"
_MIN_SECRET_LEN = 32  # 32 hex chars = 16 bytes; we generate 64 (32 bytes)


def resolve_jwt_secret(config: Config) -> str:
    """Return the JWT signing secret. Generates + persists one on first call
    when no override is set."""
    web_jwt = (config.web or {}).get("jwt") or {}
    explicit = web_jwt.get("secret")
    if explicit:
        if len(explicit) < _MIN_SECRET_LEN:
            log.warning(
                "auth: web.jwt.secret in config.yaml is shorter than %d chars — "
                "consider regenerating with `openssl rand -hex 32`",
                _MIN_SECRET_LEN,
            )
        return str(explicit)

    env = os.environ.get("DWELLERD_JWT_SECRET", "").strip()
    if env:
        if len(env) < _MIN_SECRET_LEN:
            log.warning(
                "auth: DWELLERD_JWT_SECRET is shorter than %d chars", _MIN_SECRET_LEN,
            )
        return env

    secret_path = Path(config.data_dir) / _SECRET_FILENAME
    if secret_path.exists():
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(
                f"auth: cannot read {secret_path}: {e}. "
                f"Either fix the perms (`sudo chown dwellerd:dwellerd {secret_path} "
                f"&& chmod 600 {secret_path}`) or remove the file to regenerate."
            ) from e
        if value:
            return value
        log.warning("auth: %s is empty — regenerating", secret_path)

    new_secret = secrets.token_hex(32)  # 64 hex chars / 256 bits
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically: tmp + rename, so a crash mid-write can't leave an
    # empty/partial file the next boot would interpret as "no secret".
    tmp = secret_path.with_suffix(".tmp")
    tmp.write_text(new_secret + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(secret_path)
    log.info(
        "auth: generated jwt.secret at %s (256-bit, mode 0600) — sessions will "
        "now persist across daemon restarts", secret_path,
    )
    return new_secret
