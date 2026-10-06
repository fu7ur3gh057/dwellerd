# Changelog

## Unreleased

### Quieter alerts (`alerts:` section)
- **Confirmation** — a change of level counts only after `confirm` (default 2)
  consecutive checks agree; a spike or one dropped request never alerts.
- **Hysteresis** — a value must fall `hysteresis`% (default 5) below a
  threshold before it counts as recovered; stops flapping.
- **Reminders** — a crit unresolved after `remind_after` (default 6h) is
  announced again with its age. Warnings are never repeated.
- **Grouping** — alerts within `group_window` seconds (default 5) go out as
  one message, worst first.
- **Quiet hours** — `quiet_hours: {start, end}`: only crit makes a sound;
  everything else is delivered silently. Nothing is dropped.
- **Unclean-start notice** — a heartbeat lets the next start report "the host
  was rebooted" or "the daemon was killed or crashed", with how long ago.

### Control
- **Mute** — `dwellerd mute 2h [pattern]`, `unmute`, `mutes`; globs such as
  `docker:*` and `log:*`. Stored in SQLite, expires by itself. Digest and
  report are never muted; no "recovered" message for an alert you never saw.
- **Telegram buttons** — 🔕 Mute 1h / 8h under every warn/crit and new-error
  alert, 👍 Got it under crit (stops reminders until the episode ends).
- **Telegram commands** — `/mute`, `/unmute`, `/mutes`, `/ack`, `/help`.
  Only the configured chat is obeyed; `telegram.admins` narrows it in groups.
  Long-polls `getUpdates` (outbound only). `telegram.interactive: false`
  turns it off.

### Operations
- **Reload** — `systemctl reload dwellerd` (SIGHUP) re-reads the config. A
  config that fails validation is refused, the old one keeps running, and a
  Telegram message says so. State is kept; nothing is re-announced.
- **`dwellerd config --validate`** (`make validate`) — bad regex, `warn >=
  crit`, missing fields, duplicate names; unknown keys as warnings.
- New `make reload`; unit gained `ExecReload`.

### Fixes
- `decide_transition` no longer raises `KeyError` on an unknown level.
- A cancelled check step can no longer record "announced" without sending.

### Notes for upgrading
- Defaults changed: `confirm: 2`, `hysteresis: 5`, `remind_after: 21600`,
  `telegram.interactive: true`. Set `confirm: 1`, `hysteresis: 0`,
  `remind_after: 0`, `interactive: false` for the old behaviour.
- The database migrates in place; known problems are not re-announced.
- Re-running `dwellerd setup` rewrites the config (a `.bak` is kept) and does
  not preserve a hand-written `alerts:` section.
- One bot token can be polled by one process; a second copy gets HTTP 409.

### Internals
- New modules: `gate.py`, `validate.py`, `bot.py`, `duration.py`,
  `notify/batcher.py`. New tables: `mutes`, `meta`, `targets`; new
  `check_state` columns for the gate. 316 tests (was 89).
