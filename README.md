# Dwellerd

A small systemd daemon that watches one Linux host and reports to Telegram.

No web UI, no task queue, no ORM. The bot has a handful of buttons and
commands for silencing alerts — nothing that touches the host. One process, one YAML
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
drowning you while it does. All of these are deliberate, and all of them are
tunable in the `alerts:` section:

- **Alert on transitions, not on states.** A check sitting at `crit` for six
  hours sends one message, plus one when it recovers. A partial recovery
  (`crit` → `warn`) stays silent — you get told when it is actually fixed.
- **Confirmation.** A change only counts after `confirm` (default 2)
  consecutive checks agree. One CPU spike or one dropped HTTP request never
  reaches your phone. Set `confirm: 1` to alert on the first bad reading.
- **Hysteresis.** A value that crossed a threshold has to fall `hysteresis`%
  (default 5) *below* it before it counts as recovered, so a check hovering
  at 79.9 / 80.1 does not alert and recover all night.
- **Reminders.** A `crit` still unresolved after `remind_after` (default 6h)
  is announced again, with how long it has been going on. `0` turns it off.
  Warnings are never repeated.
- **Grouping.** Alerts that fire within `group_window` seconds (default 5) of
  each other go out as one message, worst first, instead of a burst of
  twenty. A single alert is still just that alert.
- **Quiet hours.** `quiet_hours: {start: "23:00", end: "07:00"}`: inside the
  window only `crit` makes a sound; everything else is delivered silently
  (Telegram's no-notification mode). Nothing is dropped, so there is nothing
  to catch up on in the morning.
- **Mute and acknowledge.** Every alert has 🔕 Mute 1h / 8h buttons, and a
  crit has 👍 Got it, which stops its reminders. The same from the CLI
  (`dwellerd mute 2h`) or chat (`/mute 2h`). See below.
- **Per-container docker state.** Each container has its own level, so the
  second container to die still gets its own alert.
- **Log signatures.** Lines are normalised (numbers, UUIDs, hex, quoted
  strings become placeholders) and hashed. `connection refused id=1234` and
  `connection refused id=5678` are one kind of error: one alert, and a
  count in the digest.
- **Secret redaction.** Passwords, tokens, authorization/cookie values, JWTs
  and credentials embedded in URLs are replaced before a line is written to
  SQLite or sent to Telegram.
- **State that survives restarts.** All of the above lives in SQLite, so
  restarting the daemon does not re-announce everything currently wrong,
  does not re-report errors it already told you about, and does not forget
  a half-confirmed streak.

There is also a burst cap: more than five *new* kinds of error inside five
minutes and the rest roll into the digest instead of firing instantly. They
are still captured and still counted — only the instant alert is held back.

### Buttons and commands

Under every warning and critical alert, and every new-error alert:

```
[ 🔕 Mute 1h ] [ 🔕 Mute 8h ]
[ 👍 Got it ]                      ← crit only
```

A button mutes exactly that alert (`docker:app`, `disk-root`, `log:nginx`)
and swaps itself for "🔕 Muted until 15:30" so it cannot be pressed twice.
**Got it** acknowledges the problem: no more reminders for it. The
acknowledgement ends with the episode — a recovery, or an escalation from
warn to crit, clears it. Recoveries and grouped messages get no buttons.

Commands, from the configured chat:

```
/mute 2h [pattern]    silence alerts (default: everything)
/unmute [pattern]     lift one mute, or all
/mutes                what is muted, and until when
/ack <check>          same as Got it; without a name, lists what is wrong
/help
```

This is the only part of the daemon that listens: it long-polls Telegram's
`getUpdates` — outbound HTTPS only, no webhook, no open port. What it can
change is the daemon's own state (mutes, acknowledgements); it cannot run
anything on the host.

- Only the configured chat is obeyed. Anything from elsewhere is ignored,
  and a button press from elsewhere is refused and logged.
- In a **group**, anyone in it can press the buttons unless you list who
  may: `telegram.admins: [123456789]` (ids from @userinfobot).
  `config --validate` warns when a group has no list.
- Updates that piled up while the daemon was down are dropped at startup, so
  a button pressed yesterday is not replayed today.
- One bot token can be polled by one process. A second one (a dev copy next
  to the service) gets a 409; the daemon logs it and keeps retrying, and
  alerts still go out.
- `telegram.interactive: false` turns all of this off: no polling, no
  buttons, no commands.

### Muting from the shell

```bash
sudo -u dwellerd dwellerd mute 2h              # everything, for 2 hours
sudo -u dwellerd dwellerd mute 30m cpu         # one check
sudo -u dwellerd dwellerd mute 1d 'docker:*'   # a glob: all containers
sudo -u dwellerd dwellerd mute 1h 'log:*'      # new-error alerts only
sudo -u dwellerd dwellerd mutes                # what is muted, and until when
sudo -u dwellerd dwellerd unmute [pattern]     # lift one, or all
```

Durations are `30s`, `30m`, `2h`, `1d`, `1h30m` — a bare number is refused
rather than guessed. Alert names are the ones in `dwellerd check`
(`cpu`, `disk-root`, `unit-nginx`, `docker:app`, `http-site`) and
`log:<source name>` for new-error alerts. Quote globs so your shell leaves
them alone. A muted alert is still recorded in the history; the log digest
and the periodic report are never muted. A problem whose alert was muted
does not send a "recovered" message later about something you never saw.
Mutes are stored in the database, take effect on the next check, and expire
by themselves. Run the command as the daemon's user (or root) so the
database file keeps its owner.

### Knowing when it was not running

The daemon records a heartbeat. If the previous run never said goodbye, the
next start says so — "the host was rebooted" if the machine booted after the
last heartbeat, "the daemon was killed or crashed" otherwise — with how long
ago it was last seen. That message is never sent silently. It cannot tell
you the host is dead *right now*: nothing on a dead host can, and this
project deliberately uses no outside service.

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
  interactive: true               # buttons + /mute /ack commands (polls Telegram)
  # admins: [123456789]           # who may use them; empty = anyone in the chat

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

alerts:
  confirm: 2                      # consecutive checks before a change counts
  hysteresis: 5                   # % below a threshold to count as recovered
  remind_after: 21600             # re-announce a crit this old (0 = off)
  group_window: 5                 # seconds to merge simultaneous alerts
  # quiet_hours: { start: "23:00", end: "07:00" }

report:
  enabled: true
  interval: 10800
```

Set any percentage check to `false` to turn it off: `cpu: false`.

### Applying changes

```bash
make validate            # dwellerd config --validate: lint the file
sudo systemctl reload dwellerd     # re-read it without a restart (SIGHUP)
```

`--validate` catches the mistakes that otherwise surface at 3am: a bad
regex, `warn` above `crit`, a missing required field, a duplicate source
name, and — as warnings — unknown keys, which are nearly always typos. Exit
code 0 is fine (warnings allowed), 2 means errors.

A reload runs the same validation first. A config with errors is **refused**:
the daemon keeps running on the old one, logs why, and sends you a Telegram
message saying so. A good one replaces the checks, log sources, report and
Telegram target. Nothing is re-announced, because state lives in the
database. Log files are re-tailed from "now". `db_path` still needs a
restart.

## Commands

```
dwellerd setup       interactive wizard, writes the config
dwellerd run         run in the foreground (what systemd invokes)
dwellerd check       run every check once, print a table, exit
                     exit code: 0 all ok, 1 a warning, 2 something critical
dwellerd test        send a test alert to the configured chat
dwellerd report      build the periodic report now (--print to see it locally)
dwellerd config      show the resolved configuration (--validate to lint it)
dwellerd mute 2h     silence alerts for a while: dwellerd mute <duration> [pattern]
dwellerd unmute      lift one mute, or all of them
dwellerd mutes       list active mutes
```

Most of these are also make targets: `make check`, `make test-alert`,
`make report PRINT=1`, `make config`, `make validate`, `make reload`.

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
├── daemon.py         the scheduler: one asyncio task per check, plus logs, report,
│                     prune, heartbeat; SIGHUP reload; clean/unclean shutdown
├── state.py          decide_transition() — the rule that keeps Telegram quiet
├── gate.py           confirmation, hysteresis, reminders — pure functions
├── bot.py            Telegram buttons and /commands: long-poll, authorise, act
├── validate.py       config linting, shared by `config --validate` and reload
├── duration.py       "2h" / "1h30m" for the CLI
├── storage.py        SQLite: check_state, alerts, mutes, meta, log_signatures, log_events
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
│   ├── telegram.py   sendMessage, per-(kind, level) templates, retry on 429,
│   │                 grouped messages, silent delivery
│   └── batcher.py    collects simultaneous alerts into one message
├── report/           the periodic digest and its sections
└── wizard/           the interactive setup: probe the host, ask, write YAML
```

The concurrency model in full: each check gets one task running
`sleep → run → gate → maybe alert`; each log source gets one task; the
report and the prune get one each. That is it. `Restart=always` handles the
rest.

## Development

```bash
make test          # 316 tests
make run           # uses ./config.yaml and ./data/
```

Tests cover the transition rules and the alert gate (confirmation,
hysteresis, reminders), mutes, acknowledgements and quiet hours, the bot
(authorisation, forged payloads, polling backoff and backlog), grouping,
config parsing and validation, reload and rollback, the heartbeat, log signatures, storage
(including migrating a database from before the gate existed), the docker
state classifier, message rendering, and the log pipeline end to end.

## License

MIT.
