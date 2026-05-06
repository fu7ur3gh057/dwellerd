# Dwellerd

Lightweight server monitoring daemon with a hardened web admin. Watches your host (CPU/memory/disks/HTTP/systemd), tails docker compose / journalctl / file logs with dedup, sends alerts to Telegram, and exposes a JWT-protected API + Socket.IO for a dashboard SPA.

Clean rewrite of [Blackbox](https://github.com/fu7ur3gh057/Blackbox) with:

- **FHS-correct permission model** — runs as a dedicated `dwellerd` system user (not your shell user, not root by default).
- **Improved DB schema** — composite indexes, WAL, singleton CHECK constraint, FIFO-pruning safety belt.
- **Hardened JWT auth** — persistent secret, refresh-token rotation, server-side revocation, constant-time login.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton + install pipeline + permission model | ✅ done |
| 2 | Core domain (`core/{checks,notifiers,report,logs}/`) | ✅ done |
| 3 | Service infra (`db/`, `services/{taskiq,redis}/`, `tasks/`) | ✅ done |
| 4 | Web layer (`web/{application,auth,apis/*,sockets/}`) | ✅ done |
| 5 | Next.js client port + tests + polish | 🔜 pending |

Backend is feature-complete and end-to-end smoke-tested. See `CHANGES.md` for the phased build log.

## Quick start

### Dev mode

```bash
git clone <repo> Dwellerd
cd Dwellerd
make setup            # interactive wizard: language, Telegram, install confirm
make run              # foreground worker
make run-web          # foreground worker + FastAPI on 0.0.0.0:8765
```

In dev mode, data lives in `./data/`, logs in `./logs/`, config at `./config.yaml`. All owned by your shell user.

### Production install (systemd)

```bash
make bootstrap-user   # sudo: useradd dwellerd + groups: docker, systemd-journal, adm
make preflight        # verify dwellerd user can reach docker / journalctl / dirs
make install-service  # writes /etc/dwellerd/config.yaml + /etc/systemd/system/dwellerd.service, enables + starts
make status | logs    # systemctl shortcuts
make uninstall-service [--purge]   # stop/remove unit; --purge also drops user + /var/lib/dwellerd + /etc/dwellerd
```

In production:
| What | Path | Owner | Mode |
|---|---|---|---|
| Code + venv | `<wherever you cloned>` | your shell user | – |
| Config | `/etc/dwellerd/config.yaml` | `root:dwellerd` | 640 |
| Data + JWT secret | `/var/lib/dwellerd/{data,logs}/` | `dwellerd:dwellerd` | 750 |
| Systemd unit | `/etc/systemd/system/dwellerd.service` | root | 644 |

### Global CLI shim

```bash
make install-cli      # drops a `dwellerd` shim into ~/.local/bin/
dwellerd help
dwellerd setup | run | start | stop | status | logs | preflight | …
```

## Permission model

The unique part of this rewrite. The systemd unit runs as `User=dwellerd`, **not** your shell user and **not** root by default. The wizard:

1. Creates the system user: `useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/dwellerd dwellerd`
2. Adds it to (only the existing ones of) `docker`, `systemd-journal`, `adm` so it can talk to Docker, read journalctl, and read `/var/log/*`.
3. Creates `/var/lib/dwellerd/{data,logs}` (`dwellerd:dwellerd 750`) and `/etc/dwellerd/` (`root:dwellerd 750`).
4. Runs **preflight diagnostics** from the perspective of the `dwellerd` user — `sudo -u dwellerd docker info`, `journalctl -n1`, dir writability — and prints the exact fix command for every failure.

The systemd unit deliberately omits `ProtectSystem`/`PrivateTmp` because they create a private mount namespace that hides bind-mounts the daemon may need (e.g. `/var/www/<panel-user>/` on FastPanel/ISPmanager hosts). It does set `ReadWritePaths=/var/lib/dwellerd` and `NoNewPrivileges=yes`.

For control-panel hosts where `/var/www/<panel-user>/` ACLs block any non-owner, `make install-service` accepts `--as-root` as opt-in fallback.

`make fix-perms` chowns `./data` + `./logs` back to the current shell user when switching from systemd back to dev mode.

## Configuration

Single YAML file. Wizard generates it; you can also edit by hand. Path priority:

- Production: `/etc/dwellerd/config.yaml`
- Dev: `./config.yaml`

Schema (see `deploy/config.example.yaml` for the full annotated version):

```yaml
notifiers:
  - type: telegram
    bot_token: "..."
    chat_id: "..."
    lang: ru                # ru | en

checks:
  - type: cpu       ; name: cpu      ; interval: 60 ; warn_pct: 80 ; crit_pct: 90
  - type: memory    ; name: memory   ; interval: 60 ; warn_pct: 80 ; crit_pct: 90
  - type: disk      ; name: disk-root; interval: 60 ; path: /  ; warn_pct: 80 ; crit_pct: 90
  - type: http      ; name: api      ; interval: 60 ; url: https://… ; expect_status: 200
  - type: systemd   ; name: nginx    ; interval: 60 ; unit: nginx.service

report:
  interval: 2700
  hostname: my-server
  notifier: telegram
  host: { memory: {}, swap: {}, cpu: {}, disks: { paths: ["/"] }, net: { interfaces: [eth0] } }
  docker:
    - compose: /opt/myapp/docker-compose.yaml
      containers: [app, db]
      starred: [app]

logs:
  notifier: telegram
  notify: true            # set false to keep logs DB-only (no Telegram blast)
  digest_interval: 3600
  storage: { retention_days: 30, max_rows: 1000000 }
  sources:
    - type: file ;    name: nginx-error ; path: /var/log/nginx/error.log ; pattern: ".+"
    - type: docker ;  name: app ;         compose: /opt/myapp/docker-compose.yaml ; service: app ; pattern: "ERROR|Traceback"
    - type: journal ; name: nginx-syslog ; unit: nginx.service ; pattern: "(?i)error|warn"

web:
  enabled: false
  host: 0.0.0.0
  port: 8765
  prefix: /dwellerd       # all routes mount under here; "" for root mount
  jwt:
    # Auto-generated to <data_dir>/jwt.secret (0600) on first run if not set.
    # Override here only if you want a config-managed secret instead.
    # secret: "..."
    access_ttl_seconds: 1800            # 30 min — default
    refresh_ttl_seconds: 2592000        # 30 days
  user:                   # first-admin seed; copied to `users` table once
    username: admin
    password_hash: "$2b$..."            # hash via deploy/scripts/hash_password.py (TODO Phase 5) or python -c
  terminal:
    enabled: false        # in-browser shell — sensitive; see TerminalAuditEntry
    token_ttl: 1800
    allow_users: [fuad]   # whitelist (empty = any system user)

db:
  path: /var/lib/dwellerd/data/dwellerd.sqlite   # default — auto-resolves from data_dir
```

After first boot the YAML's `notifiers/checks/report/logs/web.terminal` blocks are imported into the SQLite `settings` table; from then on the DB is the source of truth and the YAML is consulted only for boot-time fields (`db.path`, `web.host/port/prefix/jwt`).

## Auth — what's hardened over Blackbox

The single thing the user explicitly asked to "make max secure". Detailed here so you don't have to read the diff.

| Concern | Blackbox | Dwellerd |
|---|---|---|
| JWT secret persistence | `secrets.token_hex()` regenerated each restart if missing in YAML | Persisted to `<data_dir>/jwt.secret` (0600, atomic write); survives systemd restart |
| Required claims | `sub`, `iat`, `exp` | + `iss=dwellerd`, `aud=dwellerd-web` (or `-terminal`), `nbf`, `jti`, `sid` — all required at decode |
| Algorithm | `HS256` only ✓ | same |
| Token TTL | single 7-day token, no revocation | access JWT 30 min + opaque refresh 30 days, **rotated** on every refresh |
| Revocation | impossible until natural expiry | DB-backed `sessions` table; logout / disable-user → next request fails (30s cache TTL); enforced on **both** REST + Socket.IO |
| Username enumeration | possible (verify_password short-circuits when row=None — fast vs slow timing) | constant-time: `verify_password_constant_time` always runs bcrypt against a process-startup dummy hash. Smoke verified 2ms delta |
| Throttle | per-IP only | per-IP **+** per-username (defeats "many proxies, one target user" pivot) |
| Cookie SameSite | `Lax` | `Strict` by default (env-overridable to `Lax`/`None`) |
| Cookie scope | `bb_session` at `/` | `dw_access` at `/`, `dw_refresh` scoped to `/api/auth` (not sent on every API call) |
| Cookie name | `bb_session` (single) | `dw_access` + `dw_refresh` (split — different lifetimes, different paths) |
| Password length | unbounded | Pydantic `min_length=1, max_length=128` (caps DoS via giant request body, prevents bcrypt 72-byte truncation surprise) |
| Bcrypt cost | implicit default | explicit `gensalt(rounds=12)` |
| Terminal token | `aud=` not used (custom `kind` claim only) | own audience `dwellerd-terminal`; `decode_terminal_token` strictly rejects access tokens and vice versa |
| Active-user check | trusts JWT until expiry | re-validates `User.is_active` on every protected request via cache |
| Sockets auth | decode JWT only | decode + sessions revocation lookup |

Env knobs:
- `DWELLERD_JWT_SECRET` — overrides config and file (useful in containers)
- `DWELLERD_BEHIND_TLS=1` — flips cookie `Secure` flag and silences the plain-HTTP warning
- `DWELLERD_COOKIE_SAMESITE=strict|lax|none` — default `strict`
- `DWELLERD_TRUST_PROXY=1` — read client IP from first `X-Forwarded-For` entry (otherwise direct peer)
- `DWELLERD_TERMINAL_DISABLED=1` — env-level kill switch for the in-browser shell (overrides config)
- `DWELLERD_BROKER_URL=redis://…` — swap InMemoryBroker for Redis (requires `pip install taskiq-redis`)

## API surface

All routes mount under `<prefix>/api/` (default prefix `/dwellerd`).

**Public:**
- `GET /api/status` — service identity
- `POST /api/auth/login` — `{username, password}` → `{access_token, …}` + sets cookies
- `POST /api/auth/refresh` — rotates refresh, returns new access (cookie auth)
- `POST /api/auth/logout` — revokes server-side session
- `GET /api/auth/me` — current user info (requires auth)
- `GET /health` — `{"status":"ok"}` for healthchecks

**Auth required (any logged-in user):**
- `GET /api/system` — host snapshot (CPU, mem, swap, load, uptime, disks)
- `GET /api/system/location` — egress IP + best-effort geo
- `GET /api/checks` — all configured checks + last result
- `GET /api/checks/{name}/history` — paginated check_results
- `POST /api/checks/{name}/run` — fire one tick on demand
- `GET /api/alerts` — alerts timeline
- `POST /api/reports/preview` — render a report digest now
- `GET /api/logs/recent` — paginated log_events
- `GET /api/logs/signatures` — dedup signature table
- `GET /api/notifiers` — list configured notifiers (no secrets)
- `POST /api/notifiers/{type}/test` — send a test message
- `GET /api/config` — read-only YAML view (Blackbox compat; mostly the DB now)

**Admin only (role=admin):**
- `GET /api/docker` — list compose projects (configured + their services)
- `GET /api/docker/standalone` — standalone containers
- `GET /api/docker/discovered` — `docker compose ls` output (auto-discovery)
- `POST /api/docker/monitor` / `DELETE /api/docker/monitor/{project}` — start/stop monitoring a compose project
- `POST /api/docker/{project}/{action}` — `up | down | restart | start | stop | pull` (project-level)
- `POST /api/docker/{project}/{service}/{action}` — same per-service
- `POST /api/docker/standalone/{name}/{action}` — `start | stop | restart`
- `GET /api/terminal/status` + `POST /api/terminal/unlock` — PAM-second-step for the in-browser shell
- `GET|POST|PATCH|DELETE /api/users` — full user CRUD; `/me/password` for self-service
- `GET|PATCH /api/settings/{docker,logs,checks,notifiers}` — runtime config edits

**Socket.IO** (`<prefix>/ws/socket.io`):
- `/alerts` — `alert:fired`
- `/checks` — `check:result` (per-check filtered via subscribe), `checks:tick`
- `/docker` — `docker:tick` (snapshot) + `docker:event` (stream)
- `/logs` — `log:line` (per-source filtered), `log:digest`
- `/system` — `system:tick`
- `/terminal` — PTY duplex (admin only; both `dw_access` cookie AND PAM-issued terminal token required)

`/api/docs` for the live OpenAPI / Swagger UI.

## Architecture

```
server/
├── main.py                 # entry: load config, init broker, run scheduler + log processor + uvicorn
├── config.py               # YAML → CheckConfig/NotifierConfig dataclasses
├── core/                   # domain (no DB / no broker imports — pure functions)
│   ├── state.py            # severity transition logic (ok/warn/crit)
│   ├── i18n.py             # localized date / uptime helpers
│   ├── checks/             # cpu, memory, disk, http, systemd_unit
│   ├── notifiers/          # base + telegram (HTML rendering)
│   ├── report/             # builder + sections (vps, docker, postgres, dlq, recent_errors)
│   └── logs/               # store, processor (signature dedup), sources (file, docker, journal)
├── db/
│   ├── models.py           # CheckResult, AlertEvent, CheckStateEntry, LogSignatureEntry, LogEvent,
│   │                       # TerminalAuditEntry, Settings, User, Session
│   ├── lifetime.py         # async engine + WAL/foreign_keys/synchronous PRAGMAs
│   ├── migrations.py       # idempotent YAML→DB seed
│   └── deps.py             # session provider for both TaskIQ and FastAPI
├── services/
│   ├── taskiq/             # broker, scheduler, lifetime (init_broker/shutdown_broker), context, deps
│   └── redis/              # placeholder (used when DWELLERD_BROKER_URL=redis://)
├── tasks/
│   ├── checks.py           # @broker.task run_check — DB write + alert dispatch on transition
│   ├── alerts.py           # @broker.task send_alert — fan out to enabled notifiers
│   ├── logs.py             # notify_log_first / notify_log_digest / prune_log_events
│   ├── report.py           # build_and_send_report
│   └── db_prune.py         # hourly retention for check_results / alerts / terminal_audit
└── web/
    ├── application.py      # FastAPI factory + Socket.IO mount + client static fallback
    ├── lifetime.py         # bg tasks (ws tickers, docker events)
    ├── auth/
    │   ├── secret.py       # JWT secret resolver (config → env → data_dir/jwt.secret)
    │   ├── passwords.py    # bcrypt + constant-time verify
    │   ├── tokens.py       # encode/decode access + terminal JWT, refresh-token primitives
    │   ├── sessions.py     # DB-backed refresh-token CRUD + 30s revocation cache
    │   └── lifetime.py     # init_auth — wire secret + cookie flags + terminal config to broker.state
    ├── apis/
    │   ├── deps.py         # require_auth (JWT + sessions check + cached active-user)
    │   ├── router.py       # public / protected / admin sub-routers
    │   ├── auth/           # login, refresh, logout, me — with throttle + constant-time
    │   ├── status/         # /api/status (public)
    │   ├── system/, checks/, alerts/, reports/, logs/, notifiers/, config/
    │   ├── docker/, terminal/, users/, settings/        # admin-only
    │   └── (each module: __init__.py + routes.py + schemas.py if any)
    └── sockets/
        ├── lifetime.py     # init_socketio: register namespaces, mount ASGI app under <prefix>/ws
        ├── namespaces.py   # AuthedNamespace base + per-namespace classes
        ├── terminal.py     # /terminal namespace: PTY fork + setuid + audit
        ├── tickers.py      # periodic snapshot pushes (system:tick, docker:tick, …)
        ├── emitter.py      # `emit(namespace, event, data)` reachable from any task
        └── deps.py         # FastAPI dependency for the live AsyncServer instance
```

## What's next (Phase 5)

1. Port `client/` from Blackbox (Next.js 15, Tailwind). Adjust:
   - Auth flow needs a refresh interceptor (Blackbox client only knows about access cookie; Dwellerd uses access + refresh + auto-rotation).
   - Cookie names: `bb_session` → `dw_access` (and add `dw_refresh` handling for /api/auth).
   - API base URL: `/blackbox/api/` → `/dwellerd/api/`.
   - Login response shape adds `role`; `/me` adds `user_id` + `sid`.
2. Port `tests/` from Blackbox.
3. CHANGES.md polish, screenshots, docker-compose example for full-stack-deploy.

## License

MIT (matching Blackbox).
