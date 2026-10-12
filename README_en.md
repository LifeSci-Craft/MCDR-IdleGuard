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
================= Idle Guard v1.1.0 ==================
#  Server    Role      State     Players  Note        Switch
1  Survival  local     running   1/20     @Survival   [Sleep]
2  Creative  remote    sleeping  0        @Creative   [Wake]
======================================================
```

In game the same data is shown, every cell has a hover tooltip, and the switch at the end is clickable.

## Installation

1. Put `idle_guard-1.1.0.mcdr` into the `plugins/` folder of **the MCDR of every backend server**
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
| `!!ig check` | config self-check: is the bus up, are two entries sharing an address, is a server set to auto sleep with nobody managing it |
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

Config file: `config/idle_guard/config.json`. Options added by an upgrade are filled in with defaults.
**In most setups you only ever change two things: `servers[]` and `bus.token`.**

The file is grouped by purpose: `sleep` / `startup` / `announce` / `fake_server` / `bus` / `servers`.
The grouping only affects **how the file looks**: the older flat layout is still read, so nothing has to be
migrated by hand.

```jsonc
{
  "instance_name": "auto",       // this instance's name; auto = the name of the server it owns
  "language": "auto",            // UI language: auto follows MCDR, or zh_cn / en_us
  "debug": false,                // log every poll round to the console (troubleshooting)

  "sleep": {                     // === when to sleep ===
    "idle_timeout": 3600,        // sleep after this many seconds without players
    "min_uptime": 300,           // grace period after a server becomes ready
    "check_interval": 600,       // poll interval (seconds)
    "state_tick": 5,             // state heartbeat (seconds), keeps the UI fresh within seconds
    "wake_cooldown": 15,         // minimum gap between two connection-triggered wakes
    "confirm_timeout": 30,       // how long !!ig confirm stays valid
    "busy_patterns": ["(?i)\\bbacking up\\b", "..."],   // while busy (backup), no automatic sleep
    "busy_hold_seconds": 300,    // postpone automatic sleep for this long after one "busy" hit
    "ignore_players": []         // player names (regex) to ignore when counting
  },

  "startup": {                   // === starting and stopping ===
    "startup_timeout": 300,      // wait this long for the server to become ready
    "stop_timeout": 180,         // wait this long for the server to stop
    "port_release_timeout": 60,  // wait this long for the port to be free before starting
    "fallback_startup_seconds": 60,  // ETA shown to players before any measurement exists
    "hot_startup_window": 300,   // restarting within this window counts as a "hot" start
    "max_wake_failures": 3       // lock automatic waking after this many failed starts
  },

  "announce": {                  // === notifications ===
    "enabled": true,
    "tell_attempt_players": true,     // tell the player who was turned away (plus a sound)
    "sound": "minecraft:block.note_block.pling",
    "player_attempt_window": 600      // remember "who tried to join" for this long
  },

  "fake_server": { "enabled": true },   // the stand-in server holding the port while asleep

  "bus": {                       // === cross-instance link ===
    "bind": "auto",              // address this instance listens on; auto copies servers[].bus
    "token": "random on first run",   // shared secret: every instance must match
    "timeout": 3.0
  },

  "velocity_config": "auto",     // import the server list from velocity.toml; "off" disables it

  "servers": [                   // === the server list: the part you actually edit ===
    { "name": "Survival", "host": "127.0.0.1", "port": 30000,
      "role": "auto", "bus": "127.0.0.1:25600" },
    { "name": "Creative", "host": "127.0.0.1", "port": 30001,
      "role": "auto", "bus": "127.0.0.1:25601" }
  ]
}
```

### Top level

| Option | Default | Notes |
|---|---|---|
| `instance_name` | `"auto"` | Name of this instance (used in logs and on the bus). `auto` = the name of the server this instance owns; rarely needs setting |
| `language` | `"auto"` | `auto` follows MCDR; `zh_cn` / `en_us` also work |
| `debug` | `false` | Log every poll round to the console |
| `velocity_config` | `"auto"` | Path to Velocity's `velocity.toml`. `auto` searches near the MCDR working directory; `off` disables it |

### `sleep`: when to sleep

| Option | Default | Notes |
|---|---|---|
| `idle_timeout` | `3600` | Sleep after this many seconds without players; overridable per server |
| `min_uptime` | `300` | Grace period after the server becomes ready. A wake is covered by it too |
| `check_interval` | `600` | Poll interval in seconds |
| `state_tick` | `5` | State heartbeat in seconds (only for servers this instance owns); `0` disables it |
| `wake_cooldown` | `15` | Minimum gap between two connection-triggered wakes (seconds) |
| `confirm_timeout` | `30` | How long `!!ig confirm` stays valid (seconds) |
| `busy_patterns` | see below | Matching lines mark the server as "busy" (backup / save) and **automatic** sleep is postponed; `[]` disables it |
| `busy_hold_seconds` | `300` | How long one "busy" hit postpones automatic sleep (it expires by itself) |
| `ignore_players` | `[]` | Player names (regex) to ignore when counting, e.g. bots |

The default `busy_patterns` cover the usual backup-plugin wording; the vanilla `Saving the game` line is
deliberately excluded (it takes milliseconds, so postponing a round for it would be pointless).

### `startup`: starting and stopping

| Option | Default | Notes |
|---|---|---|
| `startup_timeout` | `300` | How long to wait for the server to become ready (seconds) |
| `stop_timeout` | `180` | How long to wait for the server to stop (seconds) |
| `port_release_timeout` | `60` | How long to wait for the port to be free before starting (seconds) |
| `fallback_startup_seconds` | `60` | ETA shown to players before any real measurement exists (seconds) |
| `hot_startup_window` | `300` | Restarting within this window counts as a "hot" start (the ETA is kept in cold / hot buckets) |
| `max_wake_failures` | `3` | Lock automatic waking after this many failed starts |

### `announce`: notifications

| Option | Default | Notes |
|---|---|---|
| `announce.enabled` | `true` | Master switch: no "slept / started" broadcast when off |
| `announce.tell_attempt_players` | `true` | Tell the player who was turned away |
| `announce.sound` | `minecraft:block.note_block.pling` | Sound played to that player (empty string = none) |
| `announce.player_attempt_window` | `600` | How long "who tried to join" is remembered (seconds) |

### `servers[]`: the server list (the part you edit)

| Option | Default | Notes |
|---|---|---|
| `name` | required | Server name, used by commands (numbers work too). Matching `velocity.toml` is easiest |
| `host` | `"127.0.0.1"` | Address of that server **as seen from this machine**. Same machine = 127.0.0.1; across machines use the other one's IP |
| `port` | `25565` | Game port. `role: auto` claims its own server by matching `server-port` in `server.properties` |
| `role` | `"auto"` | Who owns it and how it is started; see below |
| `auto_sleep` | `true` | Whether it may be stopped automatically when idle; `false` = show idle time but never stop it |
| `auto_wake` | `true` | Whether a player connection may start it again |
| `always_on` | `false` | **Always-on**: never auto-slept, and `!!ig sleep` (including requests from other instances) is refused |
| `bus` | `null` | Bus address of the instance that **owns** this server; `null` = nobody owns it (watch only) |
| `start_command` / `stop_command` / `working_directory` | `null` | Used by `role: command` only |
| `idle_timeout` | `null` | Per-server override of `sleep.idle_timeout`; `null` = inherit |

> `null` means "**this field is not set**", not "something is missing" - see
> [Why so many fields are `null`](#why-so-many-fields-are-null) below.

**The minimal way**: if every backend server has its own MCDR, you only need `name` / `host` / `port` / `bus`
(leaving `role` at its default `auto`); MCDR fills in everything else.

`role` values:

| role | Meaning |
|---|---|
| `auto` (default, recommended) | Decide automatically: the server whose `server-port` in the working directory's `server.properties` matches is "owned by this instance" |
| `self` | Force "owned by this instance" (controlled with MCDR start / stop; player counts from MCDR events) |
| `command` | This instance starts and stops it with commands (needs the three command fields) |
| `watch` | Watch only and forward announcements, never start or stop |

**Can every entry just use `auto`?** Yes, and that is the recommended way: `auto` means "whoever's server port
matches owns it", so this instance claims its own and another instance's server shows up as a remote one.
One case needs special handling:

- **A server with no MCDR of its own** (a mirror, or something another plugin starts) is never claimed: it stays
  at "watch only" and its `auto_sleep` never fires. Use `role: command` (one instance starts/stops it with
  commands) or switch `auto_sleep` off (accept that nobody stops it).
- `role: self` is only needed to **bypass port sniffing** and pin ownership; a mismatching port logs a warning.
  Unless you are troubleshooting, you never need `self`.

**Why are `host` / `port` and `bus` written separately?** They answer different questions:

| Field | Question | Used for |
|---|---|---|
| `host` + `port` | "how does **this instance** reach that server?" | status ping, port probing, the fake server holding the port (game port) |
| `bus` | "how do **other instances** reach the MCDR that owns it?" | cross-server announcements, remote start/stop (bus port) |

A game port and a bus port have nothing to do with each other, so they cannot be one field. What *could* be
merged is the other half: `bus` can move into a top-level `instances: { "Creative": "127.0.0.1:25601" }` map,
saving a line per server (and avoiding repetition when one MCDR owns several) - still on the todo list.

**`bus.bind` vs `servers[].bus`**: the first is "where **this instance** listens", the second is "where the MCDR
that **owns that server** listens". Since `bus.bind` defaults to `"auto"` (copy the `bus` of the entry this
instance owns), the address is effectively written **once**, in `servers[].bus`.

**Automatic import from Velocity (`velocity_config`)**: on startup the plugin reads the `[servers]` section of
`velocity.toml` and adds the entries that are **not yet in `servers[]`** (new entries use the defaults:
`role: auto`, auto sleep and wake allowed), then writes the result back to `config.json` for you to edit.
**Existing entries are never touched**: `role` / `auto_sleep` / `always_on` / `bus` are yours; if an address does
not match Velocity's, the plugin only logs a reminder and keeps your value. `auto` looks for `velocity.toml` next
to the MCDR working directory, in its parent, in `Velocity/` and similar spots; if nothing is found it silently
skips. Use `"off"` to disable it.

### `bus`: bus between instances

<details><summary>Options and notes</summary>

| Option | Default | Notes |
|---|---|---|
| `bus.bind` | `"auto"` | Address this instance listens on. `auto` copies the `bus` of the entry this instance owns, or write `127.0.0.1:25600`; **empty (`null` / `""`) means the same as `auto`**. A value that is not a valid address is reported and the bus will not start |
| `bus.token` | **random** | Shared secret. **A random one is filled in when the config is first generated**; you may change it, but **every instance must use the same value** or they will report an auth failure to each other |
| `bus.timeout` | `3.0` | Timeout of one request (seconds) |

Why a random secret by default: a fixed, publicly known token is no defence at all once you go across machines -
anyone with that string could connect to your bus and tell your servers to sleep or wake. A local-only instance
that nobody else contacts barely needs one, but whenever `bind` is not a loopback address (e.g. `0.0.0.0` across
machines) it is **the only defence**.

`!!ig check` prints a **fingerprint** of the current secret (8 hex digits) - when two instances cannot talk, that
tells you instantly whether it is a "different token" problem or a "that instance is not running" problem.

</details>

### Why so many fields are `null`

A freshly generated config contains quite a few `null` values (including the command fields in `servers[]`).
They mean "**this field is not set**", not "something is missing":

| Field | Meaning of `null` |
|---|---|
| `servers[].start_command` / `stop_command` / `working_directory` | Only used by `role: command`; with `auto` / `self` / `watch` the server is start/stopped the MCDR way, so these stay unused |
| `servers[].idle_timeout` | Per-server override: `null` = follow `sleep.idle_timeout` |
| `bus.bind` | `null` / `""` behaves like `"auto"`: both mean "not set", so it copies the `bus` of the entry this instance owns |

In one sentence: **`null` = use the default / not applicable here**; putting a value there means "I decide this one".

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
