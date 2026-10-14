# Idle Guard

> An MCDReforged plugin: puts the backend servers of a group to sleep when nobody plays on them, and wakes them up when a player connects.

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

**English** | [简体中文](README.md)

Requires MCDR 2.13.0+ (tested on 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0), no third-party dependencies.
Cross-server notices and remote control require one MCDR instance per backend server.

## Features

- Counts players every `sleep.check_interval` seconds and sleeps a server after `sleep.idle_timeout` without players
- While asleep a fake server holds the port: the server list shows it as sleeping, and a player connecting gets a message and triggers a start
- Broadcasts "server started (took xx seconds)" to the whole group and notifies the player who was turned away, with an optional sound
- Startup times are measured and reported as the median, split into cold and hot starts
- A server can be marked as always on (`always_on`), and automatic sleep / wake can be disabled separately
- Inter-instance bus: cross-server notices and controlling other backend servers from any instance
- Chinese and English, switchable with `language`

## Installation

1. Download `idle_guard-1.2.0.mcdr` from [Releases](../../releases) and put it in the `plugins/` folder of every backend server's MCDR
2. Run `!!MCDR plugin reload idle_guard`, or restart MCDR
3. Edit `servers` in `config/idle_guard/config.json` (see below)
4. Run `!!ig status` to check the state

A single MCDR instance also works: set the servers it should control to `role: command` and fill in
`start_command` / `stop_command`.

## Commands

`!!ig` and `!!idleguard` are equivalent; every subcommand requires MCDR permission level 3.

| Command | Description |
|---|---|
| `!!ig` | Help page |
| `!!ig status` | Server list: number / name / role / state / players / note / switch |
| `!!ig sleep <number / name>` | Sleep a server (requires `!!ig confirm`; automatic sleeping does not) |
| `!!ig wake <number / name>` | Wake a server, may be sent to another instance |
| `!!ig confirm` / `!!ig cancel` | Confirm / cancel the pending sleep |
| `!!ig lock` / `!!ig unlock <number / name>` | Suspend / resume automatic sleep and wake for that server |
| `!!ig diag` | Diagnostics: ownership / bus and peer state / sleep-check results |
| `!!ig check` | Config self-check: bus, address conflicts, servers nobody owns |
| `!!ig stats` | Sleep count, total sleep duration, wake count |
| `!!ig poll` | Run one scan immediately |
| `!!ig reload` | Reload the plugin and its config |

The server argument accepts a number (first column of `!!ig status`, in config order) or a name;
Tab completion is available on the console.

## Configuration

Config file: `config/idle_guard/config.json`. Usually only `servers` and `bus.token` need to be changed:

```json
{
  "servers": [
    { "name": "Survival", "host": "127.0.0.1", "port": 30000, "bus": "127.0.0.1:25600" },
    { "name": "Creative", "host": "127.0.0.1", "port": 30001, "bus": "127.0.0.1:25601" }
  ]
}
```

`name` / `host` / `port` are how this instance reaches that server, and `bus` is the bus address of the
MCDR that owns it. If every server has its own MCDR, `role` can be left out and everything else can keep
its default.

### Top level

| Option | Default | Description |
|---|---|---|
| `instance_name` | `"auto"` | Name of this instance, used in logs and on the bus. `auto` = the name of the server it owns |
| `language` | `"auto"` | UI language. `auto` = follow MCDR, or set `zh_cn` / `en_us` |
| `debug` | `false` | Print per-scan details to the console |
### `velocity`

| Option | Default | Description |
|---|---|---|
| `velocity.enabled` | `true` | Whether to sync the server list from `velocity.toml` |
| `velocity.path` | `"auto"` | Path of `velocity.toml`. `auto` = look near the MCDR working directory; a relative path (to that directory) or an absolute path also works, with `~` and environment variables expanded |

`!!ig check` shows which file was read and how many servers it contained.

### `servers[]`

| Option | Default | Description |
|---|---|---|
| `name` | required | Server name, used in commands and notices |
| `host` / `port` | `"127.0.0.1"` / `25565` | Address and game port this instance uses to reach the server |
| `role` | `"auto"` | Who starts and stops it: `auto` by port / `self` through MCDR / `command` by command / `watch` watch only |
| `auto_sleep` / `auto_wake` | `true` | Whether automatic sleeping / waking on player connect is allowed |
| `always_on` | `false` | Always on: never slept automatically, and `!!ig sleep` is rejected |
| `bus` | `null` | Bus address of the MCDR that owns it; `null` = watch only |
| `start_command` / `stop_command` / `working_directory` | `null` | Only used by `role: command` |
| `idle_timeout` | `null` | Overrides `sleep.idle_timeout`; `null` = use the global value |

`auto` matches on `server-port` in `server.properties`; write `self` only when the configured port
does not match it.

### `sleep`

| Option | Default | Description |
|---|---|---|
| `idle_timeout` | `3600` | Sleep after this many seconds without players |
| `min_uptime` | `300` | Startup grace period in seconds: no idle check right after the server became ready |
| `check_interval` | `600` | Scan interval in seconds |
| `state_tick` | `5` | State heartbeat in seconds, used to refresh the displayed state quickly; `0` = off |
| `wake_cooldown` | `15` | Minimum seconds between two player-triggered wakes |
| `confirm_timeout` | `30` | Validity of `!!ig confirm` in seconds |
| `busy_patterns` | backup regexes | Automatic sleeping is suspended while the server output matches; `[]` = off |
| `busy_hold_seconds` | `300` | Seconds to stay suspended after one match |
| `ignore_players` | `[]` | Player names ignored when counting (regex), e.g. bots |

### `startup`

| Option | Default | Description |
|---|---|---|
| `startup_timeout` / `stop_timeout` | `300` / `180` | Seconds to wait for the server to be ready / to stop |
| `port_release_timeout` | `60` | Seconds to wait for the port to become bindable |
| `fallback_startup_seconds` | `60` | Estimate told to players while no measurement exists (seconds) |
| `hot_startup_window` | `300` | Hot-start window in seconds: a wake within this time after a stop counts as hot |
| `max_wake_failures` | `3` | Failed starts in a row before automatic waking is locked |

### `announce`

| Option | Default | Description |
|---|---|---|
| `enabled` | `true` | Whether to broadcast "asleep" and "started" |
| `tell_attempt_players` | `true` | Whether to notify the player who was turned away |
| `sound` | `minecraft:block.note_block.pling` | Sound played with that notice; empty string = none |
| `player_attempt_window` | `600` | Seconds to remember who was turned away |

### `fake_server` / `bus`

| Option | Default | Description |
|---|---|---|
| `fake_server.enabled` | `true` | Whether a fake server holds the port while asleep (listens on `0.0.0.0`) |
| `bus.bind` | `"auto"` | Listen address of this instance; `auto` = `servers[].bus` of the server it owns |
| `bus.token` | random | Shared secret, **must match on every instance**; `!!ig check` prints its 8-digit fingerprint |
| `bus.timeout` | `3.0` | Seconds per request |

## Notes

- MCDR has to stay running; stop the server with `stop` (`!!MCDR stop` does nothing on the console in MCDR 2.x).
- Paper requires MCDR's `handler` to be `bukkit_handler`, otherwise player events are not received.
- Do not expose the bus port to the internet; on Windows the first port takeover may trigger a firewall prompt.
- The permission level is fixed at 3; add players to `owner` / `admin` in MCDR's `permission.yml`.

Building, testing and internal design: see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

MIT
