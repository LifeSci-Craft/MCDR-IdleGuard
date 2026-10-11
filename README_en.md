# Idle Guard

> An MCDReforged plugin that puts the empty backend servers of a Velocity proxy network to sleep, and wakes them when a player tries to join.

It probes player counts every `check_interval`; once a server has been empty for `idle_timeout` the plugin lets MCDR
stop it. While asleep a small placeholder server holds the game port, so the server still shows up in the server
list with a custom message and a connection attempt triggers the automatic start. When the server is ready the whole
network is notified, the player who tried is told directly, and a sound effect can be played.

**What it never does by default**: it will not touch a server nobody is responsible for, and it will not stop a
server that has players online (or whose player count cannot be confirmed).

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.8-blue)](https://www.python.org/)

**English** | [简体中文](README.md)

| | |
|---|---|
| Plugin ID | `idle_guard` |
| Commands | `!!ig` / `!!idleguard` |
| Requires | MCDR **2.13.0+** (tested on 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0); one MCDR instance per backend server |
| License | MIT |

> Hacking on the code, or curious about the decision logic, UI rules and how things were verified?
> See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (Chinese).

## Contents

- [Features](#features)
- [Installation](#installation)
- [Commands](#commands)
- [Configuration](#configuration)
- [Caveats](#caveats)
- [License](#license)

## Features

- Discovery goes through the protocol level status ping (SLP): no plugin needed on the backend servers, no RCON
- Sleeps automatically once a server has been empty for the threshold; the threshold can be global or per server
- A placeholder server holds the port while asleep: the server list shows "sleeping", a connection gets a custom
  message and triggers the start
- When it is ready the plugin announces "server is up (took xx s)" network wide, and the player who was turned away
  gets a direct message (plus a sound) when they actually join
- The server list can be read straight from Velocity's `velocity.toml`, no need to copy names and ports by hand
- A server that should stay online can be marked **always-on** (`always_on`): never auto-stopped, and sleep commands
  are refused
- A TCP bus between instances: cross-server announcements, and you can control any backend server from any instance
  (`!!ig sleep creative`)
- Measured startup times are stored (last 8 samples); the "about xx seconds" shown to players is their median, and
  cold / hot starts are tracked separately (the longer the server was down, the slower it usually comes up)
- **No automatic sleep while the server is busy** (backing up / saving, see `busy_patterns`), so a backup is never
  cut in half
- `!!ig diag` answers, in one command, "who owns what, is the bus reachable, why has it not slept yet"
- State is checked before acting: already sleeping / busy changing state / players online are all reported instead
  of doing something pointless
- Chinese / English, following MCDR's language, with an optional per-message override
- Every command requires admin permission

The status table in the console:

```
================= Idle Guard v1.0.0 ==================
#  Server    Role      State     Players  Note        Switch
1  Survival  local     running   1/20     @Survival   [Sleep]
2  Creative  remote    sleeping  0        @Creative   [Wake]
======================================================
```

In game the same data is shown, every cell has a hover tooltip, and the switch at the end is clickable.

## Installation

1. Put `idle_guard-1.0.0.mcdr` into the `plugins/` folder of **the MCDR of every backend server**
   (one instance per server is recommended);
2. run `!!MCDR plugin reload idle_guard`, or restart MCDR;
3. the first start creates `config/idle_guard/config.json`. **Adjust `servers` to your own topology before letting
   it run**;
4. run `!!ig status` to see the state of every server.

No extra Python packages are needed, only the standard library.

A single MCDR can work too: set the entries you want it to control to `role: command` and fill in
`start_command` / `stop_command`. Note that announcements then only reach players on the server that instance
manages; cross-server announcements need one instance per backend server.

## Commands

Every subcommand requires **MCDR permission level 3 (ADMIN)**, including `help` and `status`. With a lower level
MCDR itself replies `Permission denied` and the request never reaches the plugin. `!!ig` and `!!idleguard` are
equivalent; without a subcommand the help screen is shown.

| Command | What it does |
|---|---|
| `!!ig` | help screen |
| `!!ig status` | list every server: index / server / role / state / players / note / switch |
| `!!ig diag` | diagnostics: ownership, bus and peer health, the full sleep precheck, ETA samples, recent events |
| `!!ig stats` | sleep statistics: count, total time asleep, wake count |
| `!!ig sleep <index / name>` | put it to sleep (needs `!!ig confirm` afterwards) |
| `!!ig confirm` / `!!ig cancel` | confirm / cancel the pending sleep |
| `!!ig wake <index / name>` | start it up, can be sent across instances |
| `!!ig lock` / `!!ig unlock <index / name>` | pause / resume automatic sleep and wake for that server |
| `!!ig poll` | probe once, right now |
| `!!ig reload` | reload the plugin and its config |

The server argument accepts an **index** (first column of `!!ig status`, in config order) or a name; the console has
tab completion.

`sleep` is a two step action: it asks first, and `!!ig confirm` performs it (valid for 30 seconds, only the person
who asked can confirm; in game you also get `[Sleep]` / `[Cancel]` buttons). **Automatic sleep needs no
confirmation.**

`lock` only stops the automatic logic; manual `!!ig sleep` / `!!ig wake` still work. Three consecutive failed
starts lock automatic waking automatically, and `!!ig unlock` clears that.

Sleeping and waking are asynchronous: the command first replies "putting xxx to sleep...", and the real result
follows once the server has stopped or is ready.

```
> !!ig sleep 2
Put survival to sleep? (type !!ig confirm within 30 seconds)
> !!ig confirm
confirmed, putting survival to sleep
survival is now sleeping
```

## Configuration

Config file: `config/idle_guard/config.json`. Options added in later versions are filled in with their defaults.
The option names below are the full paths inside that file.

```jsonc
{
  "language": "auto",                          // follow MCDR; zh_cn / en_us also work
  "check_interval": 600,                       // probe interval (seconds)
  "state_tick": 5,                             // state heartbeat (seconds)
  "idle_timeout": 3600,                        // sleep after this long with nobody online (seconds)
  "velocity_config": "auto",                   // read servers from velocity.toml; "off" disables it
  "servers": [
    { "name": "lobby",    "host": "127.0.0.1", "port": 25565, "role": "watch",
      "auto_sleep": false, "bus": "127.0.0.1:25600" },
    { "name": "survival", "host": "127.0.0.1", "port": 25566, "role": "auto",
      "always_on": false,                    // true = always-on, never sleeps
      "bus": "127.0.0.1:25601" }
  ],
  "bus": { "bind": "auto", "token": "change-this-token" },
  "fake_server": { "enabled": true, "bind": "0.0.0.0" },
  "announce": { "enabled": true, "warn_before_sleep": 0, "tell_attempt_players": true,
                "sound": "minecraft:block.note_block.pling" }
}
```

### Top level

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `language` | `"auto"` | `auto` follows MCDR; `zh_cn` / `en_us` are also accepted |
| `check_interval` | `600` | Probe interval (seconds). Smaller reacts faster and probes the ports more often |
| `state_tick` | `5` | State heartbeat (seconds): only the servers this instance owns, so a just-stopped / just-started server shows up within seconds; `0` disables it |
| `idle_timeout` | `3600` | Sleep after this long with nobody online. Can be overridden per server |
| `min_uptime` | `300` | Grace period after a start (seconds), so a fresh server is not judged idle |
| `grace_after_wake` | `300` | Grace period after a wake (seconds) |
| `startup_timeout` | `300` | How long to wait for the server to become ready (seconds) |
| `stop_timeout` | `180` | How long to wait for the server to stop (seconds) |
| `port_release_timeout` | `60` | How long to wait for the port to become bindable before a start (seconds) |
| `wake_cooldown` | `15` | Minimum gap between two connection-triggered wakes of the same server (seconds) |
| `fallback_startup_seconds` | `60` | ETA shown before any real measurement exists (seconds) |
| `player_attempt_window` | `600` | How long a turned-away player is remembered (seconds), for the direct notice |
| `ignore_players` | `[]` | Name patterns (regex) skipped when counting players, e.g. bots |
| `max_wake_failures` | `3` | Lock automatic waking after this many consecutive failures |
| `confirm_timeout` | `30` | Lifetime of `!!ig confirm` (seconds) |
| `velocity_config` | `"auto"` | Path of `velocity.toml`. `auto` searches next to the MCDR working directory; `off` disables it |
| `busy_patterns` | see below | Lines matching these patterns mark the server as "busy" (backup / save) and **automatic** sleep is postponed; set to `[]` to disable |
| `busy_hold_seconds` | `300` | How long one "busy" hit postpones automatic sleep (it expires by itself, no "backup finished" line needed) |
| `hot_startup_window` | `300` | What counts as a "hot start" (seconds): the ETA keeps cold / hot samples apart |
| `debug` | `false` | Print details of every probe round to the console |

</details>

### `servers[]`: the server list

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `name` | required | Server name, usable in commands (an index works too) |
| `host` / `port` | required | Address and port used for the status ping |
| `role` | `"auto"` | See the table below |
| `auto_sleep` | `true` | Allow automatic sleep (when off the idle time is still shown, it just never stops the server) |
| `auto_wake` | `true` | Allow a player connection to wake it |
| `always_on` | `false` | **Always-on**: automatic sleep never touches it, and `!!ig sleep` (including requests from other instances) is refused |
| `bus` | `""` | Bus address of the instance that owns this server, one port per instance |
| `start_command` | `null` | For `role: command` |
| `stop_command` | `null` | For `role: command` |
| `working_directory` | `null` | For `role: command`; empty means the MCDR working directory |
| `idle_timeout` | `null` | Per server override of the global threshold (seconds) |
| `startup_timeout` | `null` | Per server override of the global startup timeout (seconds) |

`role` values:

| role | Meaning |
|---|---|
| `auto` (default) | Decide automatically: the server whose `server-port` in `server.properties` of the MCDR working directory matches is "owned by this instance" |
| `self` | Owned by this instance (controlled with MCDR start / stop; player counts come from MCDR events) |
| `command` | This instance starts and stops it with commands (needs `start_command` / `stop_command` / `working_directory`) |
| `watch` | Watch only and forward announcements, never start or stop |

With `role: auto` plus `bus.bind: "auto"`, **the very same config can be copied to every MCDR**: each instance
recognises itself by the port in `server.properties` and uses the `bus` of the matching entry as its listen address.
The only column that has to differ per machine is `servers[].bus`.

**Automatic import from Velocity (`velocity_config`)**: Velocity already lists every backend server with its
address. On startup the plugin reads the `[servers]` section of `velocity.toml` and adds the entries that are
**not yet in `servers[]`** (new entries use the defaults: `role: auto`, auto sleep and wake allowed), then writes
the result back to `config.json` for you to edit. **Existing entries are never touched**: `role` / `auto_sleep` /
`always_on` / `bus` are yours; if an address does not match Velocity's, the plugin only logs a reminder and keeps
your value. `auto` looks for `velocity.toml` next to the MCDR working directory, in its parent, in `Velocity/` and
similar spots; if nothing is found it silently skips. Use `"off"` to disable.

</details>

### `bus`: bus between instances

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `bus.bind` | `"auto"` | Address this instance listens on; `auto` takes the `bus` of the entry this instance owns |
| `bus.token` | `"idle_guard"` | Shared secret, **please change it**; local network use only |
| `bus.timeout` | `3.0` | Timeout of one request (seconds) |

</details>

### `fake_server`: holds the port while asleep

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `fake_server.enabled` | `true` | When off, players see the proxy's default error and automatic waking no longer works |
| `fake_server.bind` | `"0.0.0.0"` | Address the placeholder binds to; keep it the same as the real server, otherwise players cannot connect |

</details>

### `announce`: notifications

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `announce.enabled` | `true` | Announce sleeping / starting to the whole network |
| `announce.warn_before_sleep` | `0` | Warn this many seconds before sleeping; `0` disables the warning |
| `announce.tell_attempt_players` | `true` | Send a direct message to the player who was turned away |
| `announce.sound` | `"minecraft:block.note_block.pling"` | Sound played to that player once the server is up; `""` disables it |

</details>

### Messages

Everything the player sees is **shipped inside the plugin** (`idle_guard/lang/zh_cn.json` and `lang/en_us.json`);
`config.json` contains no message text at all (`language` is only the "which language" switch).

To change a single line without touching the plugin, add an optional override layer: create `lang/zh_cn.json`
inside the data folder MCDR gives the plugin (`config/idle_guard/`, where `config.json` lives too) and write only
the keys you want to change. On startup the plugin reads the bundled file first and then applies your override.
**Without that folder the bundled messages are used as they are**; the plugin never creates or modifies it, and
upgrades will not overwrite it. Look up key names in the bundled `lang/en_us.json`.

## Caveats

- Cross-server announcements and cross-instance control need **one MCDR instance per backend server**; each bus
  port must differ, and the bus must not be exposed to the internet.
- When the player count cannot be trusted (status ping unreachable, or events and pings contradict each other) the
  plugin never sleeps the server. `role: command` servers have no MCDR events as a fallback, so make sure the
  status ping works for them.
- Fabric / vanilla servers use MCDR's default `vanilla_handler`; **Paper needs `handler: bukkit_handler`**
  (the default parsed 0% of Paper's log lines in our tests, so player events were never received).
- MCDR must stay running: once it exits, the placeholder server is gone too. Also note that `!!MCDR stop` does not
  work from the console in MCDR 2.x; use `stop` to stop the server.
- **Typing `stop` in the console is not treated as sleeping**: after a normal exit the state becomes "stopped",
  the plugin does not take over the port, and a connection will not start it again (MCDR exits together with the
  server, as usual). To get the server back into "wake on connect" mode, run `!!ig wake` and it starts up. Only an
  **abnormal exit** (crash, killed) makes the plugin take over the port and start it for connecting players.
- On Windows the first port takeover may show a firewall prompt; it has to be allowed, otherwise the system drops
  incoming player connections.
- **`@instance(offline)` in the status table** means the MCDR that owns that server cannot be reached right now
  (which is different from "nobody owns it"). `!!ig diag` tells you whether the connection was refused, the token
  mismatched, or the peer accepted but never answered.
- **No automatic sleep while a backup is running**: a line matching `busy_patterns` (the defaults cover common
  backup plugins) postpones the decision by one round. If your backup plugin prints something else, add that line
  to `busy_patterns`. Manual `!!ig sleep` is not affected by this.
- **Permissions**: the threshold is fixed at MCDR level 3 (ADMIN) and there is no permission switch in
  `config.json`. To let someone use the commands, put their name into `owner`/`admin` in MCDR's `permission.yml`.
  Player names there are **case sensitive** and matched exactly (`Paulam` is not `PaulAm`); a typo falls back to
  `default_level`, which looks like "configured it, still permission denied".

## License

MIT
