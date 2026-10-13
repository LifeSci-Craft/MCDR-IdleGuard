# Idle Guard 开发说明

面向改代码、或想弄清「判定逻辑为什么是这么写的」的人。用户向的安装 / 命令 / 配置见
[../README.md](../README.md)。

## 工作流程

```
        每 check_interval 侦察一次人数（协议层状态查询）
                    │
   有人玩 ──────────┤
                    │ 连续无人 >= idle_timeout（默认 1 小时）
                    ▼
             自动关闭该子服（MCDR stop）
                    │
                    ▼
        假服务端顶替端口（服务器列表里显示"已休眠"）
                    │
   玩家在其它子服输入 /server 该服
                    ▼
   ①玩家收到提示（"已休眠，正在自动启动，预计 xx 秒"）
   ②自动启动服务端  ③就绪后广播"已开启"，并单独提醒刚才那位玩家（带音效）
```

## 目录结构

| 文件 | 职责 |
|---|---|
| `__init__.py` | 插件入口：加载配置、注册命令、把 MCDR 事件转发给 `Supervisor` |
| `supervisor.py` | 状态机与调度：侦察、空闲判定、休眠 / 唤醒、跨实例总线服务端、自愈 |
| `commands.py` | 命令树（`!!ig`）、二次确认、状态表与帮助页的渲染调用 |
| `ui.py` | 框线 / 单元格 / 浮窗宽度模型，控制台与游戏内两种渲染共用 |
| `mcproto.py` | Minecraft 协议编解码：状态查询（SLP）、握手与登录包解析 |
| `fakeserver.py` | 休眠期间顶替游戏端口的假服务端：应答状态查询、接住登录并触发唤醒 |
| `bus.py` | 实例间 TCP 总线（带 token 鉴权） |
| `config.py` | 配置数据类与默认值 |
| `velocity.py` | 读 `velocity.toml` 的 `[servers]` 段（找文件 + 解析，不依赖 tomllib） |
| `i18n.py` + `lang/*.json` | 多语言文案与覆盖机制 |

## 设计要点与理由

**为什么是 MCDR 插件而不是 Velocity 插件。** Velocity 的默认行为是「玩家已在某个子服时，切服失败
只通知玩家、留在原子服」（官方 Javadoc + 实机验证），所以 MCDR 侧就能完成全部四件事：侦察、关服、
提示、自动开服加通知，不需要给用户增加编译 Java 插件或装代理插件的负担。

**侦察走协议层状态查询。** 只需要端口可达，不用在子服装任何东西、也不用开 RCON。

**空闲判定用双信号。** MCDR 的玩家进出事件（精确到名字）加状态查询人数，任一说不为空就不关服。
原因：插件重载、漏事件、服务端隐藏玩家列表等情况都不该导致误关有人的服。

**但 MCDR 的事件本身可能漏。** 新版 Minecraft 把进出消息写成 `System chat: Pau1am left the game`，
而 MCDR 2.16.0 的 `vanilla_handler` 对 `X left the game` 用的是 `fullmatch`，前缀一多就匹配不上，
`on_player_left` 永远不触发（进服那条走的是 `logged in with entity id`，只有"离开"会漏）。
插件因此做两件事：

1. `Supervisor.on_server_output`（挂在 MCDR 的 `on_info` 上）自己再解析一遍服务端输出里的
   进 / 退消息，兼容 `System chat: ` 前缀、经典格式与 `lost connection`；玩家的聊天行
   （`info.is_user` / `info.is_player`）一律跳过，避免有人靠打字操纵计数；
2. `_drop_stale_players` 兜底自愈：状态查询成功、连续 `STALE_PLAYER_SECONDS`（60 秒）都报
   0 人在线、且这台服"变空"发生在"最后一次确认有人"之后，才清掉那个名字并记 warning。
   一次 0 人结论、中途查到过有人、查询本身不通，都绝不动手（不能动摇"信号矛盾时先不动"这条线）。

**假服务端顶替端口。** 只有接住玩家的登录握手，才能给自定义提示并触发开服。它同时应答服务器列表
查询（MOTD 显示"已休眠"），并在响应里带 `_idle_guard` 自定义字段供插件自识别。

**抢端口要快要稳。** 服务端刚退出时端口常还没被系统释放（Windows 上残留连接会让 bind 一直失败），
插件 0.25 秒一次重试；3 秒仍抢不到、且确认端口上没有真实服务端在应答时，用 `SO_REUSEADDR`
强行接管；再不行就挂后台线程持续重试。反向守卫 `_can_start_fake`：只要真实服务端还活着，
绝不抢先绑端口。Windows 的 bind 语义还要注意：端口被 `0.0.0.0` 占着时绑 `127.0.0.1` 也会成功，
所以假服务端与总线在 Windows 上**不用** `SO_REUSEADDR`（POSIX 上要用，绕开 TIME_WAIT）。

**实例间总线。** 每个实例只能 `tellraw` 自己管理的服，所以"某服休眠了"要通知全群组、以及单独提醒
某个玩家，都靠各实例互相传话。远程操作只发给**对端**（排除自己），否则本实例会先回一句
"本实例不负责 X"把真结果盖掉。

**配置文件按功能分组（只在文件层面）。** `config.py` 里的字段一律平铺（代码与测试直接读属性），
写进 `config.json` 时由 `CONFIG_LAYOUT` + `Config.serialize()` 归成 `sleep` / `startup` / `announce` /
`fake_server` / `bus` / `servers` 几段，读回来时 `Config.deserialize()` 先摊平。这么做有两个好处：
老配置（平铺写法）**照原样能读**（不必写迁移、不会丢值），而且加字段时不用改 309 个测试。
`announce` 是"真子对象 + 布局组"重叠的那一个（多塞了 `player_attempt_window`），两个方向都要保留
它自己的字段（`tests/test_config_defaults.py` 有专门的用例盯着）。

**四个被删掉的字段（别加回来）**：`grace_after_wake`（并进 `min_uptime`）、`servers[].startup_timeout`
（只留全局）、`fake_server.bind`（固定 `0.0.0.0`）、`announce.warn_before_sleep`（功能一并删除）。
理由都是"多一行就多一个要判断要不要改的地方"，而它们要么有更强替代、要么几乎没人改。

**配置由各实例共享。** `role: auto` 通过 MCDR 工作目录下 `server.properties` 的 `server-port`
自动认出"我是谁"，`bus.bind`（默认 `"auto"`，留空等价）取该条目的 `bus` 地址作为自己的监听地址，
于是同一份配置可以原封不动复制到每个子服。地址因此**只写在 `servers[].bus` 一处**：
`servers[].bus` 回答"负责这台子服的那台实例在哪"，`bus.bind` 回答"我自己在哪听"。

两个默认值是踩过坑才改的，别改回去：

- `bus.bind` 默认曾是空的 → 新生成的配置**必然**总线不启动，用户得先读懂 `auto` 这个约定才可能填对
  （症状是"某个子服一直只监视"，线上查了两轮）。现在留空按 `auto` 处理，老配置升级即自动具备该行为。
- `bus.token` 默认曾是一个写死的公开口令 → 跨机器部署等于没有防线。现在首次生成配置时由
  `new_bus_token()` 现生成随机串；`LEGACY_BUS_TOKEN` 只留给自检识别老配置。

**启动完成判定用多信号。** MCDR 的 `is_server_startup()` 在 Paper 上实测返回 False，
所以判定 = MCDR 标志 或 状态查询可用 或（进程活着且端口能接受 TCP 连接）。

**命令回调绝不阻塞。** MCDR 的命令回调跑在它自己的任务线程上，同步等 10 秒会报
`TaskExecutor 线程已经 10 秒没有响应了` 并卡住所有命令。所以 sleep / wake 一律转到后台线程，
先回"正在执行…"，完成后再回结果。

**远程请求必须在总线超时内回真实结果。** 别的实例发来的休眠请求，先在本实例**同步**跑一遍
`precheck_sleep`（状态 + 能不能休眠），不能就当场把原因答回去；只有确实可以执行才转后台。
否则拒绝理由只会留在被请求实例的日志里，请求方要对着状态轮询到 `stop_timeout + 30` 秒超时，
玩家最后看到的是"等待超时"而不是"这台服上还有人"。

**`lock` 与失败熔断。** `lock` 把一台子服的自动休眠与自动唤醒都暂停（手动命令不受影响）。
它的硬需求来自熔断：某台服连续启动失败 3 次会被自动锁定，必须用 `!!ig unlock` 解开，
否则只能改配置或重启插件。

## 状态与角色

状态取值：`unknown` / `offline` / `starting` / `running` / `stopping` / `sleeping` / `stopped`。
本实例负责的子服由轮询与事件维护，其余子服以状态查询结果为准。

`stopped` 与 `sleeping` 的区别（别混）：`sleeping` 是**插件自己**让它睡的，假服务端占着端口，
玩家连接会唤醒；`stopped` 是**外部**停的（后台敲 `stop`、服务端自己优雅退出），插件不接管端口、
也不会替玩家开服。判定依据是服务端的退出码：0 记 `stopped`，非 0（崩溃 / 被强杀）才走
"假服务端接管 + 留住 MCDR"的兜底。服务端重新起来（`on_server_startup`）或插件明确休眠 / 唤醒时，
`external_stop` 标记清掉，恢复托管。

角色的含义（`role`）：`self` = 本实例用 MCDR 启停；`command` = 本实例用命令启停；
`watch` = 只监视；`auto` = 按 `server.properties` 端口自动判定。前两者才是 `MANAGED_ROLES`。

状态权威性：`live_views()` 返回 `(merged, live)`。别的实例说它在管某台服时，整份采用对方快照并计入
`live`；对方不在线才退回本地缓存，但那属于不可信数据，不能用来拦操作。

## 界面标准

每一屏都是一个「框」：顶栏印插件名与版本、底线印这一屏的提示，两条同宽，宽度按本屏最宽的一行现算，
内容居中、两侧用金色 `=` 填满，无论中间放什么，两侧都留至少 3 个 `=`。

状态表 7 列：编号 / 子服 / 角色 / 状态 / 人数 / 备注 / 开关。空闲时长、阈值与剩余时间放在**状态**
那一格的悬停浮窗里（单独一列基本只有个 `-`，白占宽度）。

- 编号列：`1..N` 按配置顺序，命令里可直接当参数用。
- 角色列：`本实例` / `对方实例`（远端负责）/ `只监视`。远端的 `role: self` 含义是"对方才是负责方"，
  照抄会让两行都写"本实例"，所以显示成"对方实例"。
- 备注列：`@实例名`，本实例负责的那行也会显示自己；锁定时是红色的 `[锁]`。
- 开关列：休眠中给绿色 `[开启]`、运行中给红色 `[休眠]`，点一下执行对应命令；状态正在变动时显示
  黄字且不可点。
- 配色按"这一块是什么"：金色 = 顶栏与底线、aqua = 可点或本实例在管、绿 = 运行中、红 = 有问题、
  黄 = 正在变动、灰 = 只是信息。没有一处按文本内容猜颜色。
- 控制台按显示宽度补齐（汉字算 2 列），行上印数字、底线是素线；游戏内按字宽模型补齐，细节全进
  浮窗，浮窗手工断行（每行不超过 32 显示列）。
- 聊天与通知里的插件名前缀统一是 `[Idle Guard]`，颜色统一为天蓝色 `§b`。

## 别的行为约定

- **休眠要二次确认**：`!!ig sleep` 先登记，`!!ig confirm` 才执行（`confirm_timeout` 默认 30 秒，
  按发起人隔离，自动休眠不走确认）。执行前与确认时各侦测一次状态，已经在目标状态 / 中间态 /
  有人在线都直接回话。
- **有人在线绝不休眠**：`_can_sleep` 只在 `_effective_players()` 为 0 时放行；有人或返回 None
  （事件与查询矛盾、查不到人数）一律拒绝，且拒绝时不会真的去停服。
- **玩家名必须过滤**：`safe_player_name` 用 `^[A-Za-z0-9_]{1,16}$` 校验，名字来自客户端握手，
  会拼进 `tellraw` / `playsound` 命令。
- **音效要带 `execute at`**：写成 `execute at <player> run playsound <sound> master <player> ~ ~ ~ 1 1`，
  漏掉 `execute at` 时声源落在世界原点，玩家离得远就听不见。
- **常驻子服的判定点有两处**：`_check_idle` 的 `sleep_allowed`（不自动关）与 `_can_sleep`
  （手动 / 远程 / 空闲三条路都经过它）。命令层 `_precheck_message` 也提前拦一次，
  好让管理员在输入 `!!ig confirm` 之前就知道关不了。`always_on` 会随 `ServerView` 一起过总线，
  别的实例的状态表也能显示「常驻」。
- **子服清单同步只在启动时做一次**：`Supervisor.__init__` 里 `_sync_servers_from_velocity()`
  先跑，再 `_compile_config()`（因为 `role: auto` 的判定要靠这些条目）。原则是**只补不改**：
  缺的补上、已有条目一个字段都不动、地址对不上只 warning；补过之后才写回 `config.json`，
  平时不动用户的配置文件。
- **启动耗时落盘复用**：本次实测 → 每台服最近 8 次样本（`config/idle_guard/startup_seconds.json`）
  的中位数 → `fallback_startup_seconds`。用中位数而不是"上一次"，是因为启动耗时抖动很大
  （世界加载、mod 初始化、磁盘缓存冷热），照搬上一次经常差出好几倍。旧格式（单个数字）照样能读。
- **唤醒完成的提示要能"存起来"**：玩家是被假服务端挡回客户端的，唤醒完成时他不在任何服务端里，
  `announce.opened` 的广播与给他的 `tell` 都送不到。所以触发过唤醒的玩家会进
  `ManagedServer.pending_greetings`，等他 `on_player_joined` 时补一条「已开启」+ 音效
  （超过 `player_attempt_window` 才进来就不补了）。
- **停止要分「正常」与「异常」两路**：`on_server_stop` 按退出码分流：0（后台 `stop`、服务端自己
  优雅退出）记 `stopped`，不接管端口、不动 `EXIT_AFTER_STOP` 标记（管理员就是想用 `stop` 把实例关掉）；
  非 0（崩溃 / 被强杀）才调 `set_exit_after_stop_flag(False)` 留住 MCDR，并让假服务端接管端口、
  等玩家连接时自动开服。轮询每 tick 都会重算状态，所以这个结论必须由 `ManagedServer.external_stop`
  这个标记承载，否则下一次心跳又会把它推成 `offline` → 假服务端 → `sleeping`。
- **空闲计时与 `auto_sleep` 解耦**：`_check_idle` 照常维护 `empty_since`（浮窗要显示），
  只有 `role in MANAGED_ROLES and auto_sleep and not locked` 时才真的关服。

## 文案与语言

玩家可见的文字都在插件的 `idle_guard/lang/<语言>.json`（内置 `zh_cn` 与 `en_us`），`config.json` 里没有文案，
只有 `language` 决定用哪一份（`auto` = 跟随 MCDR）。缺键会退回英文，再缺就用键名本身显示。

要改其中若干句而不动插件本体：在 `config/idle_guard/lang/` 下建一个同名文件（例如 `zh_cn.json`），
只写要改的键；启动时先读内置文件，再用这份覆盖同名键。插件不会创建也不会修改该目录，升级不会覆盖它。
键名对照插件包内的 `lang/en_us.json`。

## 一台 MCDR 管多台子服

每个子服各跑一个 MCDR 是最省心的拓扑（跨服通知天然准确、每台状态都由它自己算），但 MCDR 也是常驻进程
（每个大约 100~150 MB）。子服不多、或者不想开那么多进程时，可以让**一台 MCDR 管多台子服**：把那些条目
写成 `role: command`，由这一个实例用命令启停它们。

```jsonc
"servers": [
  { "name": "Survival", "host": "127.0.0.1", "port": 30000, "role": "command",
    "start_command": "start-survival.bat", "stop_command": "stop-survival.bat",
    "working_directory": "D:/servers/survival" },
  { "name": "Creative", "host": "127.0.0.1", "port": 30001, "role": "command",
    "start_command": "start-creative.bat", "stop_command": "stop-creative.bat",
    "working_directory": "D:/servers/creative" }
]
```

几个要点：

- `stop_command` 要能**优雅**停服（例如向那个服务端的控制台发 `stop`，或调它自己的停止脚本）。
  别用 `taskkill /F` 之类的强杀：世界来不及保存，插件这边也会把它当成"异常停止"。
- 这类子服**没有 MCDR 事件兜底**，人数完全来自状态查询。所以务必让它们开着 `enable-status=true`，
  否则插件"数不清人"时会保守地拒绝自动休眠（这是有意的安全行为，不是 bug）。
- 总线：一台 MCDR 只有一个 `bus.bind`，所以它管的几条 `role: command` 条目共用同一个实例地址；
  别的实例要操作这些子服，把它们的 `servers[].bus` 都指向这一个地址。
- 代价要说清楚：这台 MCDR 一挂，它管的**全部**子服同时失去自动启停能力；跨服通知也只能进到
  它管的那些服务端里。子服多于两三台时，还是一台一个实例更稳。

## 打包与测试

```bash
# 单元 / 集成测试（不需要 Minecraft）
python -m pytest tests/ -q

# 压测 smoke：驱动 50 轮状态心跳，核对不崩、无异常日志（不需要 Minecraft）
python tools/soak.py --rounds 50

# 静态检查（配置见仓库根的 ruff.toml）
python -m ruff check .

# 打包（剥离注释与 docstring、时间戳固定、可复现）+ 产物自检
python tools/build.py
python tools/check_artifact.py

# 跨版本矩阵：每个 MCDR 版本起一个真实 MCDR，装插件跑三条命令并逐项核对
python tools/mcdr_matrix.py
python tools/mcdr_matrix.py --reuse      # 复用已有实例目录（不做删除动作）

# 端到端场景（需要上一级的 ../test-network/ 那套真实群组服已启动）
python ../test-network/start_all.py --wait 180
python ../test-network/scenario_idle_guard.py   # 报告：../test-network/logs/e2e_report.md
python ../test-network/stop_all.py
```

打包白名单：包根只放 `mcdreforged.plugin.json` / `requirements.txt` / `CHANGELOG.md` / `LICENSE` 与
`*.py`，`lang/` 只收 `.json`。README 与开发文档不进包（体积大、对运行无用）。

## 已验证 / 未验证

已实测：

- 单元 / 集成测试 **349 项全部通过**（`python -m pytest tests/`）：协议编解码、SLP 查询、假服务端
  应答与登录打断、总线鉴权与收发（含两个真实例互发指令）、命令层（控制台来源、异步不阻塞、
  reload 的两条提示）、界面标准（顶栏与底线同宽、底线提示居中、帮助页两列对齐、浮窗每行不超过
  32 列、开关列可点）、多语言（选语言 / 同语族兜底 / 缺键退回英文 / 用户覆盖文件 / 两份语言文件
  键一致）、空闲到休眠到假服务端接管到连接触发唤醒再到二次休眠的完整状态机、端口抢不回来的
  重试与强制接管、编号寻址与 Tab 补全（走 MCDR 自己的 `CommandManager.suggest_command`）、
  状态快照权威性、执行前状态预检、状态心跳（刷新本实例与"只监视"的行、可关闭、事件驱动立即置状态、
  意外停止时阻止 MCDR 退出、空闲计时与 `auto_sleep` 解耦）、音效提醒、二次确认生命周期、
  权限（遍历命令树确认每条命令都要求 admin，并用 `execute_command` 验证普通玩家拿不到状态表）、
  休眠限制、启动耗时记录、文案一致性（中英文里前缀只有一种颜色）、自己解析服务端进 / 退消息、
  漏报离开的自愈门槛、远程休眠的同步预检，以及 `role: command` 模式（真实子进程 + 真实端口）。
  测试数量以最新一版 CHANGELOG 的验证节为准（避免改代码后这里的数字慢慢漂掉）。
- **跨版本矩阵**：MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 五个版本各起一个真实 MCDR，
  跑 `!!ig help` / `!!ig status` / `!!ig reload` 全部通过、无 Traceback；2.12.0（低于最低版本）
  被依赖检查干净卸掉。
  - 仓库内 `.workbuddy/env/matrix/` 下会留下**历史版本**的实例目录与 `run_out.txt`：在里面做全文检索时
    请用 `git grep`（直接 grep 整个目录会命中旧副本，误判成"代码没删干净"）。
- 打包产物自检：同一源码打两次字节完全相同、内容白名单、注释与 docstring 已剥离且剥离后仍可编译、
  两种安装布局都正确。
- **测试网络真机**（Velocity 3.5.1 + 2 台 Paper 1.21.4 + 2 个 MCDR 实例）：
  - 端到端场景 **38/41**（`../test-network/logs/e2e_report.md`）：空闲休眠、假服务端接管端口、跨服通知、
    连接触发唤醒、单独提醒玩家、二次休眠；
  - ⚠️ 剩下 3 项与代码缺陷无关，但需要决定怎么处理：
    ①「休眠预告送达玩家」对应的是 **v1.1.0 已删除的 `announce.warn_before_sleep`**
    （休眠前 15 秒的预告），场景断言过时 —— 要么更新场景，要么恢复该功能；
    ② 远端子服的 `sleep` 现在由**负责它的实例**判断（本实例不再用心跳缓存抢先回绝，
    否则会把该执行的操作挡掉），所以「对休眠中的服再 sleep」会先给确认提示、
    确认后才由负责方回"不用再休眠了"；本实例自己负责的子服行为不变；
    ③「对运行中的服 wake」那条抓到的是假玩家被史莱姆击杀的死亡消息（日志取样撞车），
    属场景噪声，重跑通常就会过；
  - 顶栏与底线宽度精确相等（实测都是 58 列）；跨实例动作由负责方执行并回结果（实测秒级）；
  - 玩家对着休眠中的子服输入 `/server` 收到的原文是
    `服务器 survival 已休眠，正在自动启动…预计 25 秒后可用，请稍后再用 /server survival 进入`，
    随后是 `服务器 survival 已开启 (耗时 8 秒)，可现在进入：/server survival`；
  - 音效实测：假玩家（mineflayer）日志里出现 `SOUND minecraft:block.note_block.pling`；
  - 状态心跳实测（把 `check_interval` 设成 30 秒、`state_tick` 5 秒）：直接 kill 服务端进程后
    0.5 秒状态变成"未运行"，远程唤醒就绪后另一实例在 2-4 秒内看到"运行中"。
- **用户线上环境**（Fabric 26.3 + MCDR 2.16.0，两个实例）：
  - 玩家退出后插件仍认为有人在线的问题已定位并修复。取证原文：服务端打印
    `System chat: Pau1am left the game`，插件则一直报 `Creative 还有 1 名玩家在线（Pau1am）`；
    用 MCDR 自带的正则复现过 `fullmatch('Pau1am left the game')` 命中、
    `fullmatch('System chat: Pau1am left the game')` 返回 `None`。
  - `!!ig reload` 输出 `正在重载 Idle Guard…` 加 `Idle Guard 重载完成`；
    把 `language` 改成 `en_us` 并重载后，顶栏 / 表格 / 帮助页与假服务端 MOTD 全部变成英文。

未验证（如实说明）：

- 「权限被拒」与 Tab 补全没有用假玩家真机验：测试网络的无 tty 环境下，玩家在游戏里发的 `!!`
  命令不会进 MCDR（连 `!!MCDR status` 都没回应），所以走单测验证（见上）。
- MCDR 低于 2.13.0 的环境只验证了"被干净拒绝"，没有验证功能。

## 测试网络的已知限制

这些是环境问题，不是插件缺陷，别重复踩：

- 无 tty 运行时用 `config.yml` 的 `advanced_console: false` 加
  `../test-network/tools/console_daemon.py` / `../test-network/send_cmd.py`；
  `!!MCDR stop` 无效，停服用 `stop`。
- 玩家在游戏里发的 `!!` 命令不会被 MCDR 处理，MCDR 也收不到进服事件，所以权限与事件类场景改用
  `CommandManager.execute_command` / SLP 在 pytest 里验。
- 给控制台发 `stop` 会连 MCDR 一起关掉（`EXIT_AFTER_STOP`），要模拟"只停服务端"必须按 PID 杀
  java 进程（匹配 `cwd == ../test-network/<子服>/server`）。
- 手工实验前先把 `idle_timeout` 调大（例如 900）并重载；`!!ig lock` 会连唤醒一起挡掉。
- Paper 经代理连接过之后停服约 7 秒才释放端口；选端口前先 `netstat -ano | grep LISTENING`。

## 排错

- **`!!ig sleep/wake <服>` 提示"没有实例负责 xxx"**：**一个实例都没有应答**。按 `role` 分两种情形：
  - `role: auto`：该由 `server.properties` 端口与它一致的那台 MCDR 负责。回复按这个含义给提示，
    控制台日志里还会写清试过哪些总线地址、各自为什么失败（连不上 / 鉴权失败 / 连上了却没应答）。
    逐项查：那台实例是否在运行、是否装了本插件、`servers[].bus` 是否配对。日志里搜
    `本实例负责的子服：xxx` 可以确认某个实例到底认领了谁。
  - `role: watch` 等：没有任何实例会启停它，把该条改成 `auto` 或 `self`。
- **前台看着"端口在监听"，可跨实例操作却说没人负责**：那个实例的总线接受线程可能已经不转了
  （旧版遇到意外错误会直接停摆）。日志里搜 `总线接受连接出错`。新版本会记日志并继续接受连接。
- **玩家已经退出，插件却说还有人**：看日志有没有 `状态查询已经连续 60 秒以上报"0 人在线"，
  但 MCDR 没有报告 xxx 离开`。有这行说明 MCDR 没解析出服务端的离开消息，插件已按查询结果纠正。
  连这行都没有，说明状态查询本身不通（服务端关了 `enable-status` 或端口探不通），这种"数不清人"
  的情况插件一律拒绝休眠。
- **在别的子服敲 `!!ig sleep <那台服>`，很久之后才说"等待超时"**：拒绝发生在负责那台服的实例上，
  它的日志里有 `拒绝休眠（remote，谁）` 与原因。正常情况下拒绝理由会立刻回到你这边。
- **玩家列表里显示"已休眠"但进不去**：假服务端接管失败会打印
  `端口 xxx 暂时移交不了给假服务端`（多半端口还被上一个服务端或别的程序占着），此时玩家连接会被
  拒绝；插件会挂后台线程持续重试。
- **刚关服的那几秒连不上**：端口要一会儿才被系统真正释放。插件 0.25 秒重试一次，并在 3 秒后
  （确认没有真实服务端应答时）强行接管，所以空窗通常只有一两秒。
- **改了配置 / 文案不生效**：执行 `!!ig reload` 或 `!!MCDR plugin reload idle_guard`。
- **两个实例的总线端口写成同一个**：后启动的会在日志里报 `总线绑定 127.0.0.1:xxxxx 失败`，
  跨服通知与状态查询会缺一半。每台实例给一个不同的端口。
- **服务端崩了**：插件会发现并让假服务端接管端口，玩家连接时照样能自动启动。**在后台敲 `stop`
  正常关闭**不算崩溃：状态记为「已停止」，插件不接管端口，MCDR 也会照常随服务端一起退出。
- **连续启动失败**（默认 3 次）会自动锁定自动唤醒，避免死循环刷屏，需管理员 `!!ig unlock`。
- **服务端开了 `hide-online-players`**：拿不到可信人数时不会关服。
