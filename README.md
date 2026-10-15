# Idle Guard

> MCDReforged 插件：群组服中长时间无人的子服自动休眠，有玩家连接时自动唤醒。

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

[English](README_en.md) | **简体中文**

需要 MCDR 2.13.0+（实测 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0），无第三方依赖；
跨服通知与远程操作需要每个子服各有一个 MCDR 实例。

## 功能

- 按 `sleep.check_interval` 侦察子服人数，连续无人达到 `sleep.idle_timeout` 后自动休眠
- 休眠期间由假服务端顶替端口：服务器列表显示「已休眠」，玩家连接会收到提示并触发启动
- 启动完成后向全群组广播「已开启（耗时 xx 秒）」，并单独提醒刚才被挡回的玩家，可播放音效
- 启动耗时按实测值统计，冷启动与热启动分别取中位数，用于提示里的「预计 xx 秒」
- 子服可设为常驻（`always_on`），也可分别关闭自动休眠 / 自动唤醒
- 实例间总线：跨服通知，以及在一个实例上操作其他子服
- 中英双语，用 `language` 切换

## 安装

1. 从 [Releases](../../releases) 下载 `idle_guard-1.2.3.mcdr`，放进每个子服 MCDR 的 `plugins/` 目录
2. 执行 `!!MCDR plugin reload idle_guard`，或重启 MCDR
3. 按下面的说明修改 `config/idle_guard/config.json` 的 `servers`
4. 执行 `!!ig status` 查看各子服状态

只有一台 MCDR 也能使用：把要启停的子服设为 `role: command` 并填写 `start_command` / `stop_command`。

## 命令

`!!ig` 与 `!!idleguard` 等价，全部子命令需要 MCDR 权限等级 3。

| 命令 | 作用 |
|---|---|
| `!!ig` | 帮助页 |
| `!!ig status` | 子服列表：编号 / 子服 / 角色 / 状态 / 人数 / 备注 / 开关 |
| `!!ig sleep <编号 / 子服名>` | 休眠该子服（需再输入 `!!ig confirm`；自动休眠不需要确认） |
| `!!ig wake <编号 / 子服名>` | 唤醒该子服，可跨实例下发 |
| `!!ig confirm` / `!!ig cancel` | 确认 / 取消上一次待执行的休眠 |
| `!!ig lock` / `!!ig unlock <编号 / 子服名>` | 暂停 / 恢复该子服的自动休眠与自动唤醒 |
| `!!ig diag` | 诊断：认领依据 / 总线与对端状态 / 休眠判定结论 |
| `!!ig check` | 配置自检：总线、地址冲突、无人负责的子服 |
| `!!ig stats` | 休眠次数、累计休眠时长、唤醒次数 |
| `!!ig poll` | 立即侦察一轮 |
| `!!ig reload` | 重载插件与配置 |

子服参数可填编号（`!!ig status` 第一列，按配置顺序）或名字，控制台支持 Tab 补全。

## 配置

配置文件为 `config/idle_guard/config.json`，通常只需要修改 `servers` 与 `bus.token`：

```json
{
  "servers": [
    { "name": "Survival", "host": "127.0.0.1", "port": 30000, "bus": "127.0.0.1:25600" },
    { "name": "Creative", "host": "127.0.0.1", "port": 30001, "bus": "127.0.0.1:25601" }
  ]
}
```

`name` / `host` / `port` 是本实例连接该子服的方式，`bus` 是负责该子服的 MCDR 的总线地址；
若每个子服都有自己的 MCDR，`role` 不用填，其余字段保持默认即可。

### 顶层

| 选项 | 默认 | 说明 |
|---|---|---|
| `instance_name` | `"auto"` | 本实例名，用于日志与总线。`auto` = 使用本实例负责的子服名 |
| `language` | `"auto"` | 界面语言。`auto` = 跟随 MCDR，也可填 `zh_cn` / `en_us` |
| `debug` | `false` | 在控制台输出每轮侦察的详细日志 |

下表里的选项名**就是配置文件里的完整路径**（例如 `sleep.idle_timeout`），
照着抄即可；`servers[]` 那一张是条目内的字段名。

### `velocity`

| 选项 | 默认 | 说明 |
|---|---|---|
| `velocity.enabled` | `true` | 是否从 `velocity.toml` 同步子服清单 |
| `velocity.path` | `"auto"` | `velocity.toml` 的路径。`auto` = 在 MCDR 工作目录附近查找；也可填相对路径（相对 MCDR 工作目录）、绝对路径，支持 `~` 与环境变量 |

同步结果（读了哪个文件、读到几个子服）可以用 `!!ig check` 查看。

### `servers[]`

| 选项 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | 子服名，用于命令与通知 |
| `host` / `port` | `"127.0.0.1"` / `25565` | 本实例连接该子服的地址与游戏端口 |
| `role` | `"auto"` | 由谁启停：`auto` 按端口自动判定 / `self` 用 MCDR 启停 / `command` 用命令启停 / `watch` 只监视 |
| `auto_sleep` / `auto_wake` | `true` | 是否允许自动休眠 / 玩家连接时自动唤醒 |
| `always_on` | `false` | 常驻：不自动休眠，也拒绝 `!!ig sleep` |
| `bus` | `null` | 负责该子服的 MCDR 的总线地址；同步生成的条目会自动填 `127.0.0.1:25600` 起（按名字排序），跨机器部署要改成对方 IP；`null` = 只监视 |
| `start_command` / `stop_command` / `working_directory` | `null` | 仅 `role: command` 使用 |
| `idle_timeout` | `null` | 覆盖 `sleep.idle_timeout`；`null` = 使用全局值 |

`auto` 的判定依据是 `server.properties` 里的 `server-port`；指定的端口与它不一致时才需要写 `self`。

### `sleep`

| 选项 | 默认 | 说明 |
|---|---|---|
| `sleep.idle_timeout` | `3600` | 连续无人多少秒后休眠 |
| `sleep.min_uptime` | `300` | 启动保护期（秒）：服务端就绪后这段时间内不判定空闲 |
| `sleep.check_interval` | `600` | 侦察间隔（秒） |
| `sleep.state_tick` | `5` | 状态心跳间隔（秒），用于快速刷新状态。**正数最小 0.2 秒**（写 0.1 实际按 0.2 跑）；`0` 或负数 = 关闭心跳 |
| `sleep.wake_cooldown` | `15` | 玩家连接触发的两次唤醒之间的最小间隔（秒） |
| `sleep.confirm_timeout` | `30` | `!!ig confirm` 的有效期（秒）。**最小值 5 秒**（写 2 实际给到 5 秒） |
| `sleep.busy_patterns` | 备份类正则 | 服务端输出命中时暂停自动休眠；`[]` = 关闭 |
| `sleep.busy_hold_seconds` | `300` | 命中一次后多少秒内不自动休眠 |
| `sleep.ignore_players` | `[]` | 统计人数时忽略的玩家名（正则），例如假人 |

### `startup`

| 选项 | 默认 | 说明 |
|---|---|---|
| `startup.startup_timeout` / `startup.stop_timeout` | `300` / `180` | 等待启动就绪 / 等待停止的上限（秒） |
| `startup.port_release_timeout` | `60` | 同步等待端口释放的预算（秒）：自动休眠路径实际取 `min(该值, 30)`，手动休眠路径取 `min(该值, 5)`。超时**并不是放弃** —— 转入后台持续重试（每 0.25 秒一次，最多 5 分钟后放弃并记一条 warning），这期间玩家连接仍会被拒绝。因此大于 30 不再有额外效果 |
| `startup.fallback_startup_seconds` | `60` | 尚无实测启动耗时时报给玩家的预计值（秒） |
| `startup.hot_startup_window` | `300` | 热启动判定窗口（秒）：停止后这段时间内又被唤醒算热启动 |
| `startup.max_wake_failures` | `3` | 连续启动失败多少次后锁定自动唤醒 |

### `announce`

| 选项 | 默认 | 说明 |
|---|---|---|
| `announce.enabled` | `true` | 是否广播「已休眠」与「已开启」 |
| `announce.tell_attempt_players` | `true` | 是否单独提醒被挡回的玩家 |
| `announce.sound` | `minecraft:block.note_block.pling` | 提醒音效；空字符串 = 不播放 |
| `announce.player_attempt_window` | `600` | 记录「谁被挡回过」的时长（秒） |

### `fake_server` / `bus`

| 选项 | 默认 | 说明 |
|---|---|---|
| `fake_server.enabled` | `true` | 休眠期间是否用假服务端顶替端口（监听 `0.0.0.0`） |
| `bus.bind` | `"auto"` | 本实例监听地址；`auto` = 本实例负责子服的 `servers[].bus` |
| `bus.token` | 随机串 | 总线口令，**所有实例必须一致**；`!!ig check` 显示它的 8 位指纹 |
| `bus.timeout` | `3.0` | 单次请求超时（秒） |

## 注意

- MCDR 需要保持运行；停服请用 `stop`（MCDR 2.x 的 `!!MCDR stop` 在控制台无效）。
- Paper 需要在 MCDR 配置里把 `handler` 设为 `bukkit_handler`，否则收不到玩家事件。
- 总线端口不要暴露到公网；Windows 上首次接管端口可能弹出防火墙授权，需要允许。
- 权限门槛固定为 MCDR 等级 3，在 MCDR 的 `permission.yml` 里把玩家加进 `owner` / `admin`。

构建、测试与内部设计见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## License

MIT
