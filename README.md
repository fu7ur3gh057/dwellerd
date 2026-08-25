# Dwellerd

A small systemd daemon that watches one Linux host and reports to Telegram.

No web UI, no bot commands, no task queue, no ORM. One process, one YAML
file, one chat. You run a wizard, answer a dozen questions, and the box
starts telling you when something is wrong.

```
█▀▄ █░█░█ █▀▀ █░░ █░░ █▀▀ █▀█ █▀▄
█▄▀ ▀▄▀▄▀ █▄▄ █▄▄ █▄▄ █▄▄ █▀▄ █▄▀
```

## What it watches

| Check | Alerts when |
|---|---|
| **CPU** | usage crosses `warn` / `crit` |
| **Memory** | usage crosses `warn` / `crit` |
| **Swap** | usage crosses `warn` / `crit` (silent when there is no swap) |
| **Load average** | per-core load crosses the threshold, so the numbers hold on any core count |
| **Disks** | any watched mount point fills past `warn` / `crit` |
| **HTTP** | an endpoint returns the wrong status, times out, or refuses the connection |
| **systemd units** | a unit is anything other than `active` |
| **Docker containers** | a container exits, restarts, goes unhealthy, or disappears — one alert per container |
| **Logs** | a *new kind* of error line appears in a file, a journal unit, or a container |

Plus a periodic report — the "everything is still fine, here is what the
box looks like" message you actually want when nothing has fired for hours.

## Quick start

```bash
git clone https://github.com/fu7ur3gh057/dwellerd.git
cd dwellerd

make setup            # the wizard: language, Telegram, what to watch
make check            # run every check once, see the results
make run              # foreground, Ctrl-C to stop

sudo make install     # install + start the systemd unit
make logs             # journalctl -u dwellerd -f
```

The wizard sends a real test message before it writes anything, so a wrong
token or chat id is caught in the terminal rather than at 4am.

### What you need before running it

1. A bot token from [@BotFather](https://t.me/BotFather).
2. Your chat id from [@userinfobot](https://t.me/userinfobot) — or the
   group's id (starts with `-100`) if you want the alerts in a group. Send
   the bot a message first; a bot cannot start a conversation with you.

## Staying quiet

The hard part of a monitoring daemon is not noticing problems. It is not
drowning you while it does. Four mechanisms, all of them deliberate:

- **Alert on transitions, not on states.** A check sitting at `crit` for six
  hours sends exactly one message, plus one when it recovers. A partial
  recovery (`crit` → `warn`) stays silent — you get told when it is actually
  fixed, not when it is slightly less broken.
- **Per-container docker state.** Each container has its own level, so the
  second container to die still gets its own alert instead of hiding behind
  the first one.
- **Log signatures.** Lines are normalised (numbers, UUIDs, hex, quoted
  strings become placeholders) and hashed. `connection refused id=1234` and
  `connection refused id=5678` are one kind of error: one alert, and a
  count in the digest.
- **State that survives restarts.** All of the above lives in SQLite, so
  restarting the daemon does not re-announce everything currently wrong,
  and does not re-report errors it already told you about.

There is also a burst cap: more than five *new* kinds of error inside five
minutes and the rest roll into the digest instead of firing instantly. They
are still captured and still counted — only the instant alert is held back.

## Configuration

One file. Path priority: `$DWELLERD_CONFIG`, then `/etc/dwellerd/config.yaml`,
then `./config.yaml` in the checkout.

The DB is never the source of truth for settings — you edit the YAML and
restart. See [`config.example.yaml`](config.example.yaml) for the fully
annotated version. The short shape:

```yaml
telegram:
  bot_token: "123456:ABC-DEF..."
  chat_id: "123456789"
  lang: ru                        # en | ru

hostname: my-server

checks:
  interval: 60
  cpu:    { warn: 80, crit: 90 }
  memory: { warn: 80, crit: 90 }
  swap:   { warn: 50, crit: 80 }
  load:   { warn: 2, crit: 4 }    # per core
  disks:
    - { path: "/", warn: 80, crit: 90 }
  http:
    - { name: site, url: "https://example.com", expect_status: 200 }
  systemd: [nginx.service]
  docker:
    enabled: true
    containers: []                # empty = everything docker reports

logs:
  enabled: true
  level: error                    # all | info | warn | error
  digest_interval: 3600
  retention_days: 14
  sources:
    - { type: docker_container, name: app, container: app, pattern: "ERROR|Traceback" }
    - { type: file, name: nginx, path: "/var/log/nginx/error.log", pattern: "(?i)error" }
    - { type: journal, name: sshd, unit: sshd.service, pattern: "(?i)fail" }

report:
  enabled: true
  interval: 10800
```

Set any percentage check to `false` to turn it off: `cpu: false`.

## Commands

```
dwellerd setup       interactive wizard, writes the config
dwellerd run         run in the foreground (what systemd invokes)
dwellerd check       run every check once, print a table, exit
                     exit code: 0 all ok, 1 a warning, 2 something critical
dwellerd test        send a test alert to the configured chat
dwellerd report      build the periodic report now (--print to see it locally)
dwellerd config      show the resolved configuration
```

Every one of these is also a make target: `make check`, `make test-alert`,
`make report PRINT=1`, `make config`.

## Installation model

`sudo make install` (i.e. `deploy/install.sh`):

1. Creates a `dwellerd` system user (`nologin`, no home) and adds it to
   whichever of `docker`, `systemd-journal`, `adm` exist on the host — so
   it can reach the docker socket, read the journal, and read `/var/log`.
2. Creates `/var/lib/dwellerd` (`dwellerd:dwellerd`, 750) for the database
   and `/etc/dwellerd` (`root:dwellerd`, 750) for the config, which is
   written 640 because it holds a bot token.
3. Builds the virtualenv, copies your config, and repoints `db_path` at
   `/var/lib/dwellerd` — a config written in dev mode carries a path inside
   the checkout that the service user cannot write to.
4. Verifies the service user can actually read the config and reach docker,
   and tells you the exact fix if it cannot.
5. Writes the unit, enables it, starts it, and shows the journal if it
   failed to come up.

Re-run it after a `git pull`; it is idempotent.

On control-panel hosts (FastPanel, ISPmanager) where ACLs on
`/var/www/<panel-user>/` block every non-owner, `sudo make install
ARGS=--as-root` runs the unit as root instead.

The unit deliberately does **not** set `ProtectSystem` or `PrivateTmp`:
both create a private mount namespace that hides the bind-mounts the daemon
is being asked to watch. Everything that can be locked down without
blinding it — `NoNewPrivileges`, `PrivateDevices`, `ProtectKernelTunables`,
`ProtectControlGroups`, `RestrictSUIDSGID`, `LockPersonality` — is set.

To remove it: `sudo make uninstall`, or `sudo make purge` to also drop the
user, `/etc/dwellerd` and `/var/lib/dwellerd`.

## Architecture

```
dwellerd/
├── cli.py            argparse entry point — setup | run | check | test | report | config
├── config.py         YAML -> dataclasses; every field has a default
├── daemon.py         the scheduler: one asyncio task per check, plus logs, report, prune
├── state.py          decide_transition() — the rule that keeps Telegram quiet
├── storage.py        SQLite: check_state, alerts, log_signatures, log_events
├── i18n.py           date and duration formatting, en + ru
├── checks/
│   ├── base.py       Result + the shared ok/warn/crit ladder
│   ├── host.py       cpu, memory, swap, load, disk
│   ├── service.py    http, systemd
│   └── docker.py     container state — one result per container
├── logs/
│   ├── signature.py  line normalisation + fingerprint
│   ├── processor.py  consumers, dedup, burst cap, digest
│   └── sources/      file (rotation-aware tail), journal, docker, docker compose
├── notify/
│   └── telegram.py   sendMessage, per-(kind, level) templates, retry on 429
├── report/           the periodic digest and its sections
└── wizard/           the interactive setup: probe the host, ask, write YAML
```

The concurrency model in full: each check gets one task running
`sleep → run → compare → maybe alert`; each log source gets one task; the
report and the prune get one each. That is it. `Restart=always` handles the
rest.

## Development

```bash
make test          # 76 tests
make run           # uses ./config.yaml and ./data/
```

Tests cover the transition rules, log signatures, storage (including that
first-seen survives a reconnect), config defaults, the docker state
classifier, message rendering, and the log pipeline end to end.

## License

MIT.
