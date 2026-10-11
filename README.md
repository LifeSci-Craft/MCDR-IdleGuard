# Idle Guard

> MCDReforged 插件：群组服里没人玩的子服自动休眠，有人想进时自动唤醒。

按 `check_interval` 侦察各子服人数，连续无人到 `idle_timeout` 就让 MCDR 关掉它；休眠期间由
「假服务端」顶替端口，玩家连接时会收到提示并触发自动启动。启动完成后广播给全群组、单独提醒
那位玩家，并可放一个音效。

**默认不做的事**：不会碰没人管的子服，也不会在有玩家在线（或人数无法确认）时关服。

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.8-blue)](https://www.python.org/)

| | |
|---|---|
| 插件 ID | `idle_guard` |
| 命令 | `!!ig` / `!!idleguard` |
| 需要 | MCDR **2.13.0+**（实测 2.13.0 · 2.14.1 · 2.15.0 · 2.15.7 · 2.16.0）；每个子服一个 MCDR 实例 |
| License | MIT |

> 想改代码，或了解判定逻辑、界面标准与验证方式？见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 目录

- [功能](#功能)
- [安装](#安装)
- [命令](#命令)
- [配置](#配置)
- [注意](#注意)
- [License](#license)

## 功能

- 侦察走协议层状态查询（SLP）：不需要在任何子服装插件，也不需要开 RCON
- 连续无人到阈值就自动休眠；阈值可以全局设，也可以按子服单独设
- 休眠期间假服务端顶替该端口：服务器列表里显示「已休眠」，玩家连接会看到自定义提示并触发启动
- 启动完成后广播「已开启（耗时 xx 秒）」，被挡回去的那位玩家另外收到一条提醒，可带音效
- 实例间总线：跨服通知，以及在任意一个实例上操作别的子服（`!!ig sleep creative`）
- 每次启动的实测耗时落盘复用，报给玩家的「预计 xx 秒」来自上一台服务端的真实值
- 执行前先侦测状态：已经在休眠 / 正在变动 / 有人在线，都会直接说明而不做无意义动作
- 中文 / English 双语，语言跟随 MCDR，也可以自己覆盖文案
- 命令全部需要管理员权限

控制台里的状态表：

```
================= Idle Guard v1.0.0 ==================
#  子服      角色      状态    人数  备注       开关
1  Survival  本实例    运行中  1/20  @Survival  [休眠]
2  Creative  对方实例  已休眠  0     @Creative  [开启]
======================================================
```

游戏内同一份数据，每格都可以悬停看解释，末尾的开关可以直接点。

## 安装

1. 把 `idle_guard-1.0.0.mcdr` 放进**每个子服的 MCDR** 的 `plugins/` 目录（推荐每个子服一个实例）；
2. 执行 `!!MCDR plugin reload idle_guard`，或重启 MCDR；
3. 首次启动会生成 `config/idle_guard/config.json`。**先按自己的拓扑改 `servers` 再让它跑**；
4. 执行 `!!ig status` 看各子服状态。

没有额外 Python 依赖，只用标准库。

只有一台 MCDR 也能用：把要启停的条目改成 `role: command` 并填 `start_command` / `stop_command`。
但那样只能通知到它自己管理的那个服的玩家，跨服通知需要每台子服都有实例。

## 命令

全部子命令都需要 **MCDR 权限等级 3（ADMIN）**，包括帮助页与 `status`。等级不足时由 MCDR 直接回
`权限不足`，请求不会到达插件。`!!ig` 与 `!!idleguard` 等价；不带子命令打开帮助页。

| 命令 | 作用 |
|---|---|
| `!!ig` | 帮助页 |
| `!!ig status` | 列出全部子服：编号 / 子服 / 角色 / 状态 / 人数 / 备注 / 开关 |
| `!!ig sleep <编号 / 子服名>` | 休眠（需再输入 `!!ig confirm`） |
| `!!ig confirm` / `!!ig cancel` | 确认 / 取消上一次待执行的休眠 |
| `!!ig wake <编号 / 子服名>` | 唤醒，可跨实例下发 |
| `!!ig lock` / `!!ig unlock <编号 / 子服名>` | 暂停 / 恢复这台子服上的自动休眠与自动唤醒 |
| `!!ig poll` | 立刻侦察一轮 |
| `!!ig reload` | 重载插件与配置 |

子服参数可以填**编号**（来自 `!!ig status` 第一列，按配置顺序）或名字，控制台支持 Tab 补全。

`sleep` 是两步操作：先问一句，再由 `!!ig confirm` 确认（30 秒内有效，只有发起人能确认；
游戏内同时给出 `[确认休眠]` / `[取消]` 按钮）。**自动休眠不需要确认**。

`lock` 只停自动逻辑，手动 `!!ig sleep` / `!!ig wake` 照常执行；连续启动失败 3 次会被自动锁定，
用 `!!ig unlock` 解开。

休眠与唤醒都是异步的：命令先回「正在让 xxx 休眠…」，真正停稳或启动就绪后再补一条结果。

```
> !!ig sleep 2
确认要休眠 survival 吗？（30 秒内输入 !!ig confirm 生效）
> !!ig confirm
已确认，开始休眠 survival
survival 已休眠
```

## 配置

配置文件：`config/idle_guard/config.json`。升级后新增的选项会自动按默认值补齐。
下面每张表的选项名就是文件中的完整路径。

```jsonc
{
  "language": "auto",                          // 跟随 MCDR；也可写 zh_cn / en_us
  "check_interval": 600,                       // 侦察间隔（秒）
  "state_tick": 5,                             // 状态心跳（秒）
  "idle_timeout": 3600,                        // 连续无人多久后休眠（秒）
  "servers": [
    { "name": "lobby",    "host": "127.0.0.1", "port": 25565, "role": "watch",
      "auto_sleep": false, "bus": "127.0.0.1:25600" },
    { "name": "survival", "host": "127.0.0.1", "port": 25566, "role": "auto",
      "bus": "127.0.0.1:25601" }
  ],
  "bus": { "bind": "auto", "token": "改成你自己的口令" },
  "fake_server": { "enabled": true, "bind": "0.0.0.0" },
  "announce": { "enabled": true, "warn_before_sleep": 0, "tell_attempt_players": true,
                "sound": "minecraft:block.note_block.pling" }
}
```

### 顶层

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `language` | `"auto"` | `auto` 跟随 MCDR；可写 `zh_cn` / `en_us` |
| `check_interval` | `600` | 侦察间隔（秒）。调小反应更快，也更频繁地查端口 |
| `state_tick` | `5` | 状态心跳间隔（秒）：只查本实例负责的子服，让「刚关 / 刚开」几秒内就反映到界面；`0` = 关闭心跳 |
| `idle_timeout` | `3600` | 连续无人多久后休眠（秒）。可被 `servers[].idle_timeout` 覆盖 |
| `min_uptime` | `300` | 启动后的保护期（秒），避免刚开就被判休眠 |
| `grace_after_wake` | `300` | 被唤醒后的保护期（秒） |
| `startup_timeout` | `300` | 等服务端启动就绪的上限（秒） |
| `stop_timeout` | `180` | 等服务端停稳的上限（秒） |
| `port_release_timeout` | `60` | 启动前假服务端撤下、等端口可绑定的上限（秒） |
| `wake_cooldown` | `15` | 同一台服两次连接触发的唤醒之间的最小间隔（秒） |
| `fallback_startup_seconds` | `60` | 从没实测过启动耗时之前，给玩家报的预计值（秒） |
| `player_attempt_window` | `600` | 记住「谁被挡回来过」的时长（秒），用于就绪后单独提醒 |
| `ignore_players` | `[]` | 统计时忽略的名字（正则），可用来忽略假人 |
| `max_wake_failures` | `3` | 连续启动失败几次后自动锁定 |
| `confirm_timeout` | `30` | `!!ig confirm` 的有效期（秒） |
| `debug` | `false` | 在控制台打印每轮侦察的细节 |

</details>

### `servers[]`：子服清单

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | 子服名，命令里可以直接用（也可以改用编号） |
| `host` / `port` | 必填 | 状态查询要连的地址与端口 |
| `role` | `"auto"` | 见下表 |
| `auto_sleep` | `true` | 是否允许自动休眠（关掉后仍会显示空闲时长，只是不据此关服） |
| `auto_wake` | `true` | 是否允许被玩家连接唤醒 |
| `bus` | `""` | 该子服所在实例的总线地址，每台一个端口 |
| `start_command` | `null` | `role: command` 时用 |
| `stop_command` | `null` | `role: command` 时用 |
| `working_directory` | `null` | `role: command` 时用；留空 = MCDR 工作目录 |
| `idle_timeout` | `null` | 单独覆盖全局阈值（秒） |
| `startup_timeout` | `null` | 单独覆盖全局启动上限（秒） |

`role` 的取值：

| role | 含义 |
|---|---|
| `auto`（默认） | 自动判定：端口与 MCDR 工作目录下 `server.properties` 的 `server-port` 相同的那台，就是「本实例负责」的服务端 |
| `self` | 本实例负责（用 MCDR 的 start / stop 控制，人数用 MCDR 事件精确统计） |
| `command` | 本实例用命令启停（需填 `start_command` / `stop_command` / `working_directory`） |
| `watch` | 只监视、只转发通知，不启停 |

`role: auto` 加上 `bus.bind: "auto"` 之后，**同一份配置可以原封不动复制到每台 MCDR**：
每个实例按 `server.properties` 的端口认出自己是谁，并取对应条目的 `bus` 地址当自己的监听地址。
需要为每台机器区分的只有 `servers[].bus` 这一列。

</details>

### `bus`：实例间总线

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `bus.bind` | `"auto"` | 本实例监听的地址；`auto` = 取「本实例负责的子服」那条的 `bus` |
| `bus.token` | `"idle_guard"` | 连接口令，**请改掉**；只允许本机 / 内网使用 |
| `bus.timeout` | `3.0` | 单次请求超时（秒） |

</details>

### `fake_server`：休眠期间顶替端口

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `fake_server.enabled` | `true` | 关掉后玩家连接会看到代理的默认报错，也不能自动唤醒 |
| `fake_server.bind` | `"0.0.0.0"` | 假服务端绑定的地址，需要与真实服务端一致（否则玩家连不上） |

</details>

### `announce`：通知

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `announce.enabled` | `true` | 休眠 / 开启时是否广播给全群组 |
| `announce.warn_before_sleep` | `0` | 休眠前多少秒先预告一次；`0` = 不预告 |
| `announce.tell_attempt_players` | `true` | 是否单独提醒「想进但被挡住」的那位玩家 |
| `announce.sound` | `"minecraft:block.note_block.pling"` | 服开好后放给那位玩家的音效；`""` = 不播放 |

</details>

### 文案

玩家看到的文字都**打包在插件里**（`idle_guard/lang/zh_cn.json` 与 `lang/en_us.json`），
`config.json` 里没有任何一句文案（`language` 只是"用哪种语言"的开关）。

想改某一句又不动插件本体，可以加一层可选的覆盖：在 MCDR 给本插件的数据目录
（`config/idle_guard/`，`config.json` 也在这里）下建 `lang/zh_cn.json`，只写要改的键即可。
插件启动时先读内置文件、再用这份覆盖同名键。**不建这个目录就完全用内置文案**，
插件自己不会创建也不会修改它，升级也不会覆盖。键名对照插件包的 `lang/en_us.json` 查找。

## 注意

- 跨服通知与跨实例操作需要**每个子服各有一个 MCDR 实例**；总线端口每台不同，且不要暴露到公网。
- 人数不可信时（状态查询不通，或事件与查询互相矛盾）一律不休眠。`role: command` 的子服没有 MCDR
  事件兜底，请确保状态查询可用。
- Fabric / 原版服务端用 MCDR 默认的 `vanilla_handler`；**Paper 需要 `handler: bukkit_handler`**
  （默认值对 Paper 的日志解析率实测为 0%，玩家事件会完全收不到）。
- MCDR 必须常驻：它一关，假服务端也就没了。另注意 MCDR 2.x 的 `!!MCDR stop` 在控制台无效，
  停服用 `stop`。
- Windows 上第一次接管端口时可能弹防火墙授权框，需要允许，否则玩家连接会被系统丢包。
- **权限**：命令门槛固定为 MCDR 等级 3（ADMIN），`config.json` 里没有权限开关。要放行某人就在
  MCDR 的 `permission.yml` 里把他的名字填进 `owner`/`admin`。⚠️ 那里的玩家名**大小写敏感**且精确匹配
  （`Paulam` ≠ `PaulAm`），写错会退回 `default_level`，表现为「明明配了却提示权限不足」。

## License

MIT
