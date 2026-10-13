# Idle Guard

> MCDReforged 插件：群组服里没人玩的子服自动休眠，有人想进时自动唤醒。

按 `check_interval` 侦察各子服人数，连续无人到 `idle_timeout` 就让 MCDR 关掉它；休眠期间由
「假服务端」顶替端口，玩家连接时会收到提示并触发自动启动。启动完成后广播给全群组、单独提醒
那位玩家，并可放一个音效。

**默认不做的事**：不会碰没人管的子服，也不会在有玩家在线（或人数无法确认）时关服。

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

[English](README_en.md) | **简体中文**

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
- 启动完成后广播「已开启（耗时 xx 秒）」，被挡回去的那位玩家在真正进服时会收到一条提醒，可带音效
- 子服清单可以从 Velocity 的 `velocity.toml` 自动读进来，不用手抄名字与端口
- 希望长期开着的子服可以设成**常驻**（`always_on`）：不会自动关，也不接受休眠指令
- 实例间总线：跨服通知，以及在任意一个实例上操作别的子服（`!!ig sleep creative`）
- 每次启动的实测耗时落盘（保留最近 8 次），报给玩家的「预计 xx 秒」取其中位数，
  并按「冷 / 热启动」分开统计（停机越久通常起得越慢）
- 服务端在备份 / 保存期间**不做自动休眠**（`busy_patterns`），免得把备份打断
- `!!ig diag` 一条命令看清「谁负责谁、总线通不通、为什么还不休眠」
- 执行前先侦测状态：已经在休眠 / 正在变动 / 有人在线，都会直接说明而不做无意义动作
- 中文 / English 双语，语言跟随 MCDR，也可以自己覆盖文案
- 命令全部需要管理员权限

控制台里的状态表：

```
================= Idle Guard v1.2.0 ==================
#  子服      角色      状态    人数  备注       开关
1  Survival  本实例    运行中  1/20  @Survival  [休眠]
2  Creative  对方实例  已休眠  0     @Creative  [开启]
======================================================
```

游戏内同一份数据，每格都可以悬停看解释，末尾的开关可以直接点。

## 安装

1. 把 `idle_guard-1.2.0.mcdr` 放进**每个子服的 MCDR** 的 `plugins/` 目录（推荐每个子服一个实例）；
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
| `!!ig diag` | 诊断页：认领依据 / 总线与对端健康度 / 休眠预检逐条结论 / ETA 样本 / 最近事件 |
| `!!ig check` | 配置自检：总线起没起、地址有没有撞车、有没有子服配了自动休眠却没人管 |
| `!!ig stats` | 休眠统计：次数、累计休眠时长、唤醒次数 |
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
**绝大多数情况你只需要改 `servers[]` 和 `bus.token` 两处**，其余保持默认即可。

文件按功能分成几段：`sleep`（什么时候休眠）/ `startup`（起停要等多久）/ `announce`（通知）/
`fake_server`（休眠期间的假服务端）/ `bus`（实例间通信）/ `servers`（子服清单）。
分组只影响**文件长相**：老版本那份平铺写法照样能读，不用手改。

```jsonc
{
  "instance_name": "auto",       // 本实例的名字；auto = 用「我负责的那台子服」的名字
  "language": "auto",            // 界面语言：auto 跟随 MCDR，也可写 zh_cn / en_us
  "debug": false,                // 在控制台打印每轮侦察细节（排错才开）

  "sleep": {                     // 【什么时候休眠】
    "idle_timeout": 3600,        // 连续无人多久后休眠（秒）
    "min_uptime": 300,           // 启动保护期：刚开好的这段时间内不判定空闲（秒）
    "check_interval": 600,       // 侦察间隔（秒）
    "state_tick": 5,             // 状态心跳（秒），让「刚关 / 刚开」几秒内就反映到界面
    "wake_cooldown": 15,         // 两次「玩家连接触发唤醒」之间的最小间隔（秒）
    "confirm_timeout": 30,       // !!ig confirm 的有效期（秒）
    "busy_patterns": ["(?i)\\bbacking up\\b", "..."],   // 命中就认为服务端在备份，期间不自动休眠
    "busy_hold_seconds": 300,    // 命中一次「忙」之后，往后多少秒不自动休眠
    "ignore_players": []         // 统计时忽略的名字（正则），可用来忽略假人
  },

  "startup": {                   // 【起停与等待】
    "startup_timeout": 300,      // 等服务端启动就绪的上限（秒）
    "stop_timeout": 180,         // 等服务端停稳的上限（秒）
    "port_release_timeout": 60,  // 启动前假服务端撤下、等端口可绑定的上限（秒）
    "fallback_startup_seconds": 60,  // 还没实测过启动耗时前，报给玩家的预计值（秒）
    "hot_startup_window": 300,   // 停机后这么快之内又被唤醒算「热启动」，ETA 按冷 / 热分别统计
    "max_wake_failures": 3       // 连续启动失败几次后锁定自动唤醒
  },

  "announce": {                  // 【通知】
    "enabled": true,
    "tell_attempt_players": true,     // 单独提醒「刚被挡回来」的那位玩家（含音效）
    "sound": "minecraft:block.note_block.pling",
    "player_attempt_window": 600      // 记住「谁被挡回来过」多久（秒）
  },

  "fake_server": { "enabled": true },   // 休眠期间顶替端口的假服务端（关掉就没有「连接自动唤醒」）

  "bus": {                       // 【实例间通信】
    "bind": "auto",              // 本实例监听地址；auto = 照抄「我负责那条」的 servers[].bus
    "token": "首次生成时自动填入随机串",  // 口令：所有实例必须一致
    "timeout": 3.0
  },

  "velocity_config": "auto",     // 从 velocity.toml 自动补子服清单；"off" = 关闭

  "servers": [                   // 【子服清单】这一段才是你真正要改的
    { "name": "Survival", "host": "127.0.0.1", "port": 30000,
      "role": "auto", "bus": "127.0.0.1:25600" },
    { "name": "Creative", "host": "127.0.0.1", "port": 30001,
      "role": "auto", "bus": "127.0.0.1:25601" }
  ]
}
```

### 顶层

| 选项 | 默认 | 说明 |
|---|---|---|
| `instance_name` | `"auto"` | 本实例的名字（日志与总线里用它标识自己）。`auto` = 用「本实例负责的那台子服」的名字，一般不用填 |
| `language` | `"auto"` | `auto` 跟随 MCDR；可写 `zh_cn` / `en_us` |
| `debug` | `false` | 在控制台打印每轮侦察的细节 |
| `velocity_config` | `"auto"` | Velocity 的 `velocity.toml` 路径。`auto` = 在 MCDR 工作目录附近自动找；`off` = 关闭。见下面「子服清单自动同步」 |

### `sleep`：什么时候休眠

| 选项 | 默认 | 说明 |
|---|---|---|
| `idle_timeout` | `3600` | 连续无人多久后休眠（秒）。可被 `servers[].idle_timeout` 单条覆盖 |
| `min_uptime` | `300` | 启动保护期（秒）：服务端就绪后这段时间内不判定空闲。被唤醒的场景也由它保护 |
| `check_interval` | `600` | 侦察间隔（秒）。调小反应更快，也更频繁地查端口 |
| `state_tick` | `5` | 状态心跳间隔（秒）：只查本实例负责的子服，让「刚关 / 刚开」几秒内就反映到界面；`0` = 关闭心跳 |
| `wake_cooldown` | `15` | 同一台服两次「玩家连接触发唤醒」之间的最小间隔（秒） |
| `confirm_timeout` | `30` | `!!ig confirm` 的有效期（秒） |
| `busy_patterns` | 见下 | 命中这些正则就认为服务端「正在忙」（备份 / 保存），期间不做**自动**休眠；清空成 `[]` 可关闭 |
| `busy_hold_seconds` | `300` | 命中一次「忙」之后，往后这么多秒不做自动休眠（到点自动失效，不依赖「备份结束」日志） |
| `ignore_players` | `[]` | 统计时忽略的名字（正则），可用来忽略假人 |

`busy_patterns` 默认覆盖常见的备份插件文案（`backing up` / `backup started` / `world backup created` 等），
故意不含原版的 `Saving the game`（那是几十毫秒级的例行保存，每次都延后一轮没有意义）。

### `startup`：起停与等待

| 选项 | 默认 | 说明 |
|---|---|---|
| `startup_timeout` | `300` | 等服务端启动就绪的上限（秒） |
| `stop_timeout` | `180` | 等服务端停稳的上限（秒） |
| `port_release_timeout` | `60` | 启动前假服务端撤下、等端口可绑定的上限（秒） |
| `fallback_startup_seconds` | `60` | 从没实测过启动耗时之前，给玩家报的预计值（秒） |
| `hot_startup_window` | `300` | 「热启动」的判定窗口（秒）：停机后这么快之内又被唤醒算热启动 |
| `max_wake_failures` | `3` | 连续启动失败几次后自动锁定 |

### `announce`：通知

| 选项 | 默认 | 说明 |
|---|---|---|
| `announce.enabled` | `true` | 总开关：关掉后不再广播「已休眠 / 已开启」 |
| `announce.tell_attempt_players` | `true` | 单独提醒「试图进入但被挡住」的玩家 |
| `announce.sound` | `minecraft:block.note_block.pling` | 提醒时放的音效（空字符串 = 不播放） |
| `announce.player_attempt_window` | `600` | 记住「谁被挡回来过」的时长（秒），用于就绪后单独提醒 |

### `servers[]`：子服清单（你真正要改的一段）

| 选项 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | 子服名。命令里直接用（也可用编号）；与 `velocity.toml` / 子服目录名一致最省事 |
| `host` | `"127.0.0.1"` | **从本实例这台机器看过去**该服务端的地址。同机就是 127.0.0.1；跨机器要写对方 IP |
| `port` | `25565` | 该服务端的游戏端口。`role: auto` 就是靠它和 `server.properties` 的 `server-port` 对上来自认 |
| `role` | `"auto"` | 谁负责它、怎么启停，见下表 |
| `auto_sleep` | `true` | 到点是否允许**自动**关它；`false` = 只显示空闲时长、不据此关服 |
| `auto_wake` | `true` | 玩家连接休眠中的它时，是否允许自动开起来 |
| `always_on` | `false` | **常驻**：自动判定不关它，`!!ig sleep`（含别的实例发来的）也拒绝 |
| `bus` | `null` | **负责它的那台 MCDR** 的总线地址；留 `null` = 没人负责（只能监视） |
| `start_command` / `stop_command` / `working_directory` | `null` | 仅 `role: command` 用：本实例用命令启停它 |
| `idle_timeout` | `null` | 单独给这台服设一个空闲阈值；`null` = 用 `sleep.idle_timeout` |

> `null` 的含义是「**这一格不设置**」，不是"缺了东西"。为什么会有一堆 `null`、分别是什么意思，
> 见下面「[为什么很多字段是 `null`](#为什么很多字段是-null)」。

**最省事的写法**：如果你的每个子服都有自己的一份 MCDR，那么**只填 `name` / `host` / `port` / `bus` 四个字段就够了**
（`role` 保持默认的 `auto`），其余全部省略，省略掉的字段 MCDR 会自动按默认值补上。

`role` 的取值：

| role | 含义 |
|---|---|
| `auto`（默认，推荐） | 自动判定：端口与 MCDR 工作目录下 `server.properties` 的 `server-port` 相同的那台，就是「本实例负责」的服务端 |
| `self` | 强制指定「本实例负责」（用 MCDR 的 start / stop 控制，人数用 MCDR 事件精确统计） |
| `command` | 本实例用命令启停（需填 `start_command` / `stop_command` / `working_directory`） |
| `watch` | 只监视、只转发通知，不启停 |

**能不能全部写 `auto`？** 可以，而且推荐。`auto` 的意思就是"谁的服务端端口对得上，谁负责它"：
本实例认领自己的那台，别的实例的那台自动显示成「对方实例」。只有一种情况要特殊处理：

- **某台子服没有自己的 MCDR**（例如只是镜像，或由别的插件启停）→ 没有任何实例会认领它，
  它会一直显示「只监视」，配的 `auto_sleep` 也永远不会生效。这时要么改成 `role: command`
  （指定由某一台实例用命令启停它），要么把 `auto_sleep` 关掉（承认"没人关它"）。
- `role: self` 只在你想**绕开端口嗅探、固定指定**时才需要；端口对不上时控制台会警告。
  所以除非排错，一般不必写 `self`。

**`host` / `port` 和 `bus` 为什么要各写一次？** 它们回答的不是同一个问题：

| 字段 | 回答的问题 | 用途 |
|---|---|---|
| `host` + `port` | 「**本实例**怎么连到那台服务端」 | 状态查询、探端口、假服务端顶替端口（游戏端口） |
| `bus` | 「**别的实例**怎么连到管它的那台 MCDR」 | 跨服通知、在别的实例上远程启停（总线端口） |

游戏端口和总线端口本来就无关（一个是 Minecraft 在听，一个是 MCDR 在听），所以不能合并成一处。
能合并的是另一半：`bus` 其实可以挪到顶层按实例写一次（`instances: { "Creative": "127.0.0.1:25601" }`），
这样每条子服少一行、一台 MCDR 管多台子服时也不必重复填；这条还在待办里。

**`bus.bind` 和 `servers[].bus` 的区别**：前者是「**本实例自己**在哪个地址上听消息」，
后者是「**负责那台子服的那台 MCDR**在哪个地址等消息」。因为 `bus.bind` 默认 `"auto"`
（= 照抄「本实例负责那条」的 `servers[].bus`），所以**地址实际上只在 `servers[].bus` 里写一遍**，
`bus.bind` 通常根本不用动。

**子服清单自动同步（`velocity_config`）**：Velocity 里本来就写着每个子服的名字与地址，不必再手抄一遍。
插件启动时会读一遍 `velocity.toml` 的 `[servers]` 段，把**还没写进 `servers[]`** 的条目补进来
（新条目用默认值：`role: auto`、允许自动休眠与唤醒），然后把结果写回 `config.json`，你可以接着改。
**已有条目一律不动**：`role` / `auto_sleep` / `always_on` / `bus` 这些是你手写的，若地址与 Velocity
里对不上，只会在控制台提醒一句、保留你的配置。`auto` 会在 MCDR 工作目录、上一级、`Velocity/`
等常见位置找 `velocity.toml`；找不到就安静跳过，不影响运行。不想用它就写 `"off"`。

### `bus`：实例间总线

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `bus.bind` | `"auto"` | 本实例监听的地址。`auto` = 照抄「本实例负责的那条子服」的 `bus`，也可直接写 `127.0.0.1:25600`；**留空（`null` / `""`）与 `auto` 等价**。填的若不是合法地址，会在控制台报错并不启动总线 |
| `bus.token` | **随机串** | 连接口令。**首次生成配置时自动填入随机串**，你也可以改成任意字符串，但**所有实例必须一致**，否则会互相报「鉴权失败」 |
| `bus.timeout` | `3.0` | 单次请求超时（秒） |

口令为什么默认是随机串：它一旦是公开的固定值，跨机器部署就等于没有防线：
任何拿到那串字符的人都能连上你的总线，替你对子服下达休眠 / 唤醒指令。
如果这个实例**只在本机用**、也不需要别的实例连它，其实可以不设口令；但只要 `bind` 不是回环地址
（例如跨机器部署绑了 `0.0.0.0`），它就**是唯一防线**。

`!!ig check` 会显示当前口令的**指纹**（8 位十六进制）；两个实例对不上时，用它可以一眼看出
是"口令不一致"还是"那台实例没运行"（这两种表现都是连不上，很难分）。
口令本身不会打进日志（日志经常被整段贴出来），要对照就看指纹。

</details>

### 为什么很多字段是 `null`

文件里会看到不少 `null`（尤其是 `servers[]` 里那几行）。这不是"缺了东西"，而是「**这一格不设置**」：

| 字段 | `null` 的含义 |
|---|---|
| `servers[].start_command` / `stop_command` / `working_directory` | 只有 `role: command` 用得上；其它角色（`auto` / `self` / `watch`）用 MCDR 的方式启停，这几格不需要 |
| `servers[].idle_timeout` | **单独覆盖**用：留 `null` = 跟随 `sleep.idle_timeout` |
| `bus.bind` | 留 `null` / `""` 与 `"auto"` 等价（都是"没设置 → 走自动规则"） |

之所以不能靠"删掉这几行"来让文件变短：MCDR 读配置时发现缺字段会**按默认值补全，并把整份文件重写回去**，
所以删了下次启动就会长回来。MCDR 的 JSON 也不支持注释，因此这些字段的说明只能放在这份 README 里。

一句话：**`null` = 用默认值 / 这一格不适用**，改成具体值就表示"这一格我要自己定"。

### `fake_server`：休眠期间顶替端口

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `fake_server.enabled` | `true` | 关掉后玩家连接会看到代理的默认报错，也不能自动唤醒 |

假服务端的监听地址固定 `0.0.0.0`（和真实服务端一致），不做成选项：它没有需要按机器区分的场景。

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
- **在后台敲 `stop` 停服不会被当成休眠**：服务端正常退出后状态显示「已停止」，插件不会接管端口，
  玩家连接也不会自动开服（MCDR 会照常随服务端一起退出）。想让这台子服回到「玩家连接时自动唤醒」
  的休眠态，用 `!!ig wake` 把它开起来即可。只有服务端**异常退出**（崩溃、被强杀）时，插件才会挂上
  假服务端兜底，等玩家连接时自动开服。
- Windows 上第一次接管端口时可能弹防火墙授权框，需要允许，否则玩家连接会被系统丢包。
- **状态表里的 `@实例名(离线)`**：负责它的那台 MCDR 现在联系不上（这跟「没人管它」是两回事）。
  `!!ig diag` 会说明具体是连不上、鉴权失败还是连上了没应答。
- **备份期间不会被自动休眠**：命中 `busy_patterns`（默认覆盖常见的备份插件文案）就延后一轮判定。
  你那台备份插件的文案如果不一样，把它那行加进 `busy_patterns` 即可。管理员手敲的 `!!ig sleep`
  不受这条限制。
- **权限**：命令门槛固定为 MCDR 等级 3（ADMIN），`config.json` 里没有权限开关。要放行某人就在
  MCDR 的 `permission.yml` 里把他的名字填进 `owner`/`admin`。⚠️ 那里的玩家名**大小写敏感**且精确匹配
  （`Paulam` ≠ `PaulAm`），写错会退回 `default_level`，表现为「明明配了却提示权限不足」。

## License

MIT
