# Dwellerd — build log

Phase-by-phase log of the Blackbox → Dwellerd rewrite. Final state at pause: 8917 lines, 106 .py files. End-to-end smoke verified.

## Phase 0 — Decision (2026-05-06)

User asked for a clean rewrite of Blackbox with focus on permission correctness. Discussed the trade-offs:
- **User-mode** (Blackbox default): systemd unit runs as invoking shell user. Simple, but no separation of concerns; `make run` from a different shell trips on file ownership.
- **Root mode** (`--as-root`): always works on control-panel hosts but writes everything as root.
- **Dedicated `dwellerd` system user** (chosen): clean separation, FHS layout, requires more wizard logic but the install is self-contained.

Decided on dedicated user. Codenamed **Sequoia** initially; renamed to **Dwellerd** before any domain code was ported.

## Phase 1 — Skeleton + install pipeline + permission model

**Files (14):** `pyproject.toml`, `requirements.txt`, `.gitignore`, `Makefile`, `deploy/config.example.yaml`, `deploy/scripts/{_bootstrap.sh, setup.sh, setup.py, install-service.sh, uninstall-service.sh, install-cli.sh}`, `src/{main.py, config.py}`.

**Permission model implemented in `_bootstrap.sh`:**
- `ensure_dwellerd_user` — `useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/dwellerd`, then `usermod -aG` for `docker`, `systemd-journal`, `adm` (only if those groups exist on the host)
- `ensure_writable_paths` — chowns `./data` + `./logs` to current shell user when switching from systemd back to dev mode (idempotent, prompts before sudo)
- `preflight_dwellerd` — from `dwellerd`'s perspective, runs `docker info`, `docker compose version`, `journalctl -n1 --no-pager`, dir writability tests; prints exact fix command for each failure (e.g. `sudo usermod -aG docker dwellerd && sudo systemctl restart dwellerd`)
- `ensure_venv` / `ensure_node` — same shape as Blackbox with renamed env var (`DWELLERD_YES`)

**Wizard (setup.py, minimal Phase-1 version):**
- Language pick (RU default, EN fallback)
- Telegram credentials (bot token + chat id)
- `install-service` flow that uses the new permission model (the file ownership wizard from Blackbox didn't fit — we control all of it via `dwellerd` user)
- `uninstall-service [--purge]` — without flag keeps `/var/lib/dwellerd` so retention data isn't lost; with `--purge` drops the user and both `/var/lib/dwellerd` + `/etc/dwellerd`

**Systemd unit shape:**
```
[Service]
User=dwellerd
Group=dwellerd
WorkingDirectory=<project>
Environment=PYTHONPATH=<project>/src
ExecStart=<venv>/bin/python -m main /etc/dwellerd/config.yaml
ReadWritePaths=/var/lib/dwellerd
NoNewPrivileges=yes
Restart=on-failure
```
Deliberately **no** `ProtectSystem=` / `PrivateTmp=` / `ProtectHome=` because they create a private mount namespace that hides bind-mounts to `/var/www/<panel-user>/` etc. that the daemon may need to reach. `--as-root` flag preserved as opt-in for FastPanel/ISPmanager hosts.

**Smoke:** `make help`, `make setup --help`, daemon stub starts + handles SIGTERM + exits clean.

## Phase 2 — Core domain

**Approach:** copy `Blackbox/src/core/` → `Dwellerd/src/core/`, sed-rename tokens (`BLACKBOX`/`Blackbox`/`blackbox`/`_bb_*` → DWELLERD / Dwellerd / dwellerd / `_dw_*`). Lazy DB imports inside methods (`from db.models import LogEvent`) mean module load works without `db/` existing yet.

**Ported (2295 lines, 31 files):**
- `core/state.py` — severity tracker (ok/warn/crit transitions)
- `core/i18n.py` — localized date / uptime helpers
- `core/checks/{base, cpu, memory, disk, http, systemd_unit}.py`
- `core/notifiers/{base, telegram}.py`
- `core/report/builder.py` + `core/report/sections/{vps, docker, postgres, dlq, recent_errors}.py`
- `core/logs/{base, store, processor}.py` + `core/logs/sources/{file, docker, docker_container, journal}.py`

**`config.py` rewritten** to match Blackbox shape: `CheckConfig`/`NotifierConfig` dataclasses with `options: dict` (so type-specific fields like `warn_pct`, `bot_token` go into `.options`). Kept the Phase-1 `data_dir`/`logs_dir` extension that auto-resolves to `/var/lib/dwellerd/{data,logs}` when config lives at `/etc/dwellerd/`.

**Smoke:** all 31 modules import; no runtime regressions.

## Phase 3 — Service infra + DB schema improvements

User asked: "отрефактори модели бд и улучши если надо" — refactor and improve the DB models if needed.

**Improvements over Blackbox (in `src/db/models.py`):**

| Improvement | Why |
|---|---|
| Composite indexes on hot read paths | `CheckResult(name, ts)`, `AlertEvent(name, ts)`, `LogEvent(source, ts)+(sig, ts)`, `TerminalAuditEntry(sid, ts)+(username, ts)`. Old single-column `ts`/`name` indexes removed (left-prefix covers). Cheaper than SQLite's index-merge on the typical "recent N for X" query. |
| `sqlite_autoincrement=True` on append-only tables | Without it SQLite recycles rowids after the row with `max(rowid)` is deleted — FIFO pruning that walks `id <= cutoff_id` could in theory delete recycled-id rows. Defense in depth. |
| `Settings.id` singleton enforced via CHECK | `int = Field(default=1, primary_key=True)` + `__table_args__ = (CheckConstraint("id = 1", name="settings_singleton"),)`. Blocks `INSERT id=2` at the DB level. Verified. |
| `Session` table added | New: refresh-token revocation backbone. Composite unique index on `refresh_token_hash`. Used in Phase 4 by sessions.py. |

**Improvements over Blackbox (in `src/db/lifetime.py`):**
- `event.listens_for(engine.sync_engine, "connect")` listener sets `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000` on every new connection. Blackbox didn't set these — readers blocked writers under contention.
- Default `db_path = config.data_dir / "dwellerd.sqlite"` — auto-resolves to `/var/lib/dwellerd/data/dwellerd.sqlite` in prod without config explicitly setting it.

**Ported verbatim (sed-rename only):**
- `src/db/{migrations.py, deps.py}`
- `src/services/taskiq/{broker, scheduler, lifetime, context, deps}.py` — `BLACKBOX_BROKER_URL` → `DWELLERD_BROKER_URL`
- `src/services/redis/{lifetime, deps, utils}.py`
- `src/tasks/{checks, alerts, logs, report, db_prune}.py`

**Web sockets stub** — `src/web/sockets/__init__.py` exports a no-op `async def emit(namespace, event, data)` so `tasks/checks.py` and `tasks/alerts.py` can import it without the full sockets layer existing yet. Replaced in Phase 4.

**`src/main.py` rewritten** to wire the full pipeline: `init_broker(config)` → run `scheduler` + `log_processor` + (later) `uvicorn` as gathered coroutines, with SIGINT/SIGTERM handling and shutdown notifier dispatch.

**Smoke:**
1. Empty config → `nothing to run, exiting` (exit 0), DB file created with all 9 tables, 7 composite indexes, all PRAGMAs verified inside SA session (`wal/1/1/5000`).
2. Insert `id=2` into `settings` → `CHECK constraint failed: settings_singleton` ✓
3. Config with one CPU check (interval=1s) for 3s → 3 rows in `check_results`, `check_state` updated, `alerts` empty (no transition).

## Phase 4 — Web layer + JWT hardening

User asked: "jwt делай макс безопасным если он не такой сейчас" — make JWT max secure if it isn't already.

Identified weaknesses in Blackbox auth and fixed them.

### Phase 4.1a — persistent JWT secret (`web/auth/secret.py`)

**Problem:** Blackbox does `secrets.token_hex(32)` if `web.jwt.secret` is missing. Every restart kicks every active session out. Misconfigured prod silently runs on ephemeral secret.

**Fix:** resolution order = `config.web.jwt.secret` → `DWELLERD_JWT_SECRET` env → `<data_dir>/jwt.secret`. The file is auto-generated on first run (256-bit `secrets.token_hex(32)`) with mode 0600, written atomically (tmp + rename so a crash mid-write can't leave an empty file). In prod it lives at `/var/lib/dwellerd/data/jwt.secret`, owned by the `dwellerd` user.

### Phase 4.1b — hardened tokens.py + passwords.py

**`tokens.py` over Blackbox:**
- Required claims at decode: `iss="dwellerd"`, `aud="dwellerd-web"`, `jti` (uuid4), `nbf`, `iat`, `exp`, `sub`, `sid` — `decode_access_token` uses PyJWT's `options={"require": [...]}` so missing claims raise `MissingRequiredClaimError`.
- `algorithms=["HS256"]` only (defense in depth — PyJWT defends against `alg=none` itself).
- **Separate audience for terminal tokens**: `encode_terminal_token` / `decode_terminal_token` use `aud="dwellerd-terminal"` — a leaked terminal JWT can't be used as an access JWT and vice versa.
- Refresh tokens are opaque random strings (`secrets.token_urlsafe(32)`, sha256-hashed for DB lookup — fast O(1) on the unique index, no slow KDF needed since the token has full 256-bit entropy already).

**`passwords.py` over Blackbox:**
- `verify_password_constant_time(plain, hashed_or_none)` always runs a real bcrypt compare. When the DB lookup returned None (user doesn't exist or is disabled), it runs against a process-startup-generated dummy hash. Discards the result, returns False. **Verified 2ms timing delta** between existing-user-bad-pwd (227 ms) and no-user-bad-pwd (225 ms) — username enumeration via timing infeasible.
- Explicit `bcrypt.gensalt(rounds=12)` (~250 ms on modern CPUs). Bumpable to 13 if hardware tolerates.
- `hash_password` rejects > 128 char input (matches the Pydantic Field validator on login; defends against bcrypt's silent-truncation-at-72-bytes surprise).

### Phase 4.1c — sessions table for refresh-token rotation

New `Session` SQLModel table with `user_id` FK to users.id, `refresh_token_hash` (sha256 hex, unique index), `issued_at`, `last_used_at`, `expires_at`, `ip`, `user_agent`, `revoked_at`.

`web/auth/sessions.py` provides:
- `create_session()` — INSERT row on login
- `find_active_by_refresh()` — lookup by hash, reject if revoked or expired
- `revoke_session()` / `revoke_all_for_user()` — sets `revoked_at` and clears the cache
- `lookup_session_status()` — DB lookup with **30s in-process cache** (`_status_cache: dict[sid, _CacheEntry]`); returns `(valid, user_id, username, role)`. Cached negative results too so a revoked token can't spam the DB.
- `touch_session()` — updates `last_used_at` on refresh

Cache TTL of 30s means a logged-out user's stolen access token works for at most 30 seconds (vs 30 minutes if we trusted the JWT until expiry).

### Phase 4.1d — auth routes + deps

**`web/apis/deps.py` `require_auth`** — three layers, fail-fast:
1. Extract token from `Authorization: Bearer …` header OR `dw_access` cookie
2. Decode + verify (signature, `iss`, `aud`, `jti`/`nbf`/`exp`/`sub`/`sid` required)
3. Look up `sid` in `sessions` table, reject if revoked / expired / linked user disabled (cached 30s)

Returns a flat dict `{sub, role, sid, user_id, exp, jti}` — handlers don't need to dig into broker.state.

**`web/apis/auth/routes.py`:**
- `POST /api/auth/login` — `{username, password}` (Pydantic `min_length=1, max_length=128`) → constant-time bcrypt verify → INSERT session row → mint access JWT carrying its `sid` → set both cookies. Returns `{access_token, expires_in, username, role}`.
- `POST /api/auth/refresh` — reads `dw_refresh` cookie → looks up session by hash → if active: revoke old, INSERT new, mint new access JWT, set new cookies. If revoked/expired/missing: 401.
- `POST /api/auth/logout` — revokes the session row whose `sid` is in the access JWT, clears both cookies.
- `GET /api/auth/me` — returns `{username, role, user_id, expires_at, sid}` (re-fetched via the cache, not blindly trusted from JWT claims).

**Throttle (in-process):**
- Per-IP **and** per-username dicts, sliding 15-min window
- 5 fails → exponential back-off (60 s, 120 s, 240 s, …, capped at 64×)
- Per-user dimension defeats the "many proxies, one target user" pivot
- GC keeps each dict bounded; success drops both keys
- `DWELLERD_TRUST_PROXY=1` opts into reading the first XFF entry (else direct peer wins — without the gate an attacker could spoof XFF to bypass throttle from one source)

**Cookies:**
- `dw_access`: HttpOnly, SameSite=Strict, Secure (auto-flipped via `DWELLERD_BEHIND_TLS`), path=`/`, max-age=access_ttl
- `dw_refresh`: same flags, path=`/api/auth` (not sent on every API call), max-age=refresh_ttl
- Defaults: 30 min access / 30 days refresh; configurable via `web.jwt.access_ttl_seconds` / `refresh_ttl_seconds`
- `DWELLERD_COOKIE_SAMESITE=lax|strict|none` env override (default `strict`; `none` is coerced back to `strict` if not behind TLS)

### Phase 4.2 — FastAPI shell + status route + main.py --web wiring

`web/application.py` factory: prefix-aware (`/dwellerd/api/...`, `/dwellerd/ws/...`, `/dwellerd/health`), CORS for the dev Next.js port, lifespan from `web/lifetime.py`. Phase-4.2 cut mounted only auth + status to validate the foundation. Smoke verified login/refresh/logout/me cycle with proper revocation.

`main.py` `_run_web()` — embeds uvicorn via `uvicorn.Server(cfg).serve()` in the same event loop as the worker. `server.install_signal_handlers = lambda: None` so `main` keeps ownership of SIGINT/SIGTERM. Loud warning when bound to `0.0.0.0` over plain HTTP without `DWELLERD_BEHIND_TLS=1`.

### Phase 4.3 — port remaining route modules

11 subdirs copied from Blackbox under `web/apis/`: `alerts`, `checks`, `config`, `docker`, `logs`, `notifiers`, `reports`, `settings`, `system`, `terminal`, `users`. Sed-renamed `BLACKBOX/Blackbox/blackbox` → `DWELLERD/Dwellerd/dwellerd`, `bb_session` → `dw_access`, `_bb_` → `_dw_`. 35 files, ~3500 lines.

Router updated to mount everything: public (status, auth) / protected (system, checks, alerts, reports, logs, notifiers, config) / admin (docker, terminal, users, settings).

### Phase 4.4 — sockets layer

7 files copied from Blackbox `web/sockets/`: `lifetime`, `namespaces`, `terminal`, `tickers`, `emitter`, `deps`, `__init__`. Same sed-rename. Replaces the Phase-3 stub.

5 namespaces registered: `/alerts`, `/checks`, `/docker` (admin-only), `/logs`, `/system`. `/terminal` registered conditionally based on `web.terminal.enabled` AND env kill-switch.

### Phase 4.5 — align ported code with new schema

Schema drift the sed didn't catch:
- `decode_token` (single function in Blackbox) → split into `decode_access_token` + `decode_terminal_token` here. Updated callers in `web/sockets/{namespaces, terminal}.py` and `web/apis/terminal/routes.py`.
- `encode_token({...claims}, secret, expiry)` (positional, freeform) → `encode_access_token(*, sub, role, sid, secret, expiry_seconds)` and `encode_terminal_token(*, unix_user, via_web_user, uid, secret, expiry_seconds)`. Updated terminal routes.
- Sockets `AuthedNamespace.on_connect` previously trusted the JWT until expiry. Now also calls `lookup_session_status(db, sid)` — revoked sessions can't keep streaming events.
- Terminal namespace double-validates: access token (aud=dwellerd-web) **and** terminal token (aud=dwellerd-terminal). The aud mismatch makes substitution impossible.
- `Annotated[X, Cookie(...)]` syntax doesn't work in current FastAPI — switched to `x: X = Cookie(...)` form.

### End-to-end smoke (Phase 4 final)

Live daemon on `127.0.0.1:18765`:

| Test | Result |
|---|---|
| `GET /health` | `{"status":"ok"}` ✓ |
| `GET /api/status` | `{"service":"dwellerd","version":"0.1.0"}` ✓ |
| `POST /api/auth/login` (good creds) | 313-char JWT with all claims ✓ |
| `GET /api/auth/me` (no token) | HTTP 401 ✓ |
| `GET /api/auth/me` (Bearer) | `{username, role, user_id, sid, expires_at}` ✓ |
| `POST /api/auth/refresh` (cookie) | new JWT with `sid=2` (rotation) ✓ |
| `GET /me` w/ rotated old token | HTTP 401 (sid=1 revoked) ✓ |
| **Timing**: existing user vs no user, both bad pwd | **227 ms vs 225 ms — 2 ms delta** ✓ |
| `POST /api/auth/logout` | `{"ok": true}`, session marked revoked ✓ |
| `GET /me` after logout | HTTP 401 ✓ |
| 5× wrong creds → throttle | HTTP 429 by attempt 4 ✓ |
| `data/jwt.secret` perms | `-rw-------` (0600), 65 bytes ✓ |
| `GET /api/system` (auth) | real CPU/mem/disk/load/uptime ✓ |
| `GET /api/checks` (auth, scheduler running) | `[{name:"cpu", level:"ok", last_run_ts, last_value, last_detail}]` ✓ |
| `GET /api/alerts` (auth) | shows fired alerts (CPU 100% spike) ✓ |
| `GET /api/users` (admin) | seeded admin user listed ✓ |
| `GET /api/docs` | Swagger UI ✓ |
| `GET /ws/socket.io/?EIO=4&transport=polling` | handshake JSON with sid ✓ |

50+ routes mounted across 14 modules, all lazy-loaded via FastAPI, no import errors.

## Phase 5 — pending

Next.js client port + tests + polish. See `README.md` § What's next for the migration checklist (auth-flow refresh interceptor, cookie name change, API base URL, login/me response shape additions).
