"""
配置定义

MCDR 会在 `config/idle_guard/config.json` 生成默认配置文件；改动后可用 `!!ig reload` 热重载。
同一份配置可以（也推荐）复制到每个子服的 MCDR 实例上使用：
每个实例通过 `role` / 自动端口嗅探决定"自己是哪一个"，其余条目按对等关系参与通知。

配置里**只有行为开关，没有文案**：所有面对玩家/管理员的文字（MOTD、休眠提示、
命令输出等）都在插件的 `lang/<语言>.json` 里（见 `i18n.py`），
用 `language` 字段选语言；想改文案就在 `config/idle_guard/lang/` 下放同名覆盖文件。
"""
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from mcdreforged.api.utils import Serializable

from .velocity import OFF_VALUES as VELOCITY_OFF_VALUES

#: "服务端正在忙"的默认特征：常见备份插件的开始文案。
#: 故意**不含**原版的 `Saving the game` —— 那是几十毫秒级的例行保存，
#: 每次都延后一轮判定没有意义（真想拦它自己加进 busy_patterns 即可）。
DEFAULT_BUSY_PATTERNS = (
    r'(?i)\bbacking up\b',
    r'(?i)\bbackup (?:has )?(?:started|begun|complete|completed|finished)\b',
    r'(?i)\b(?:creating|preparing|starting) (?:the )?(?:world |server )?backup\b',
    r'(?i)\bworld backup (?:started|created)\b',
)


@dataclass
class ServerEntry(Serializable):
    """一个子服务器的描述（与 velocity.toml 里的服务器名保持一致最省事）。"""

    #: 子服名，用于通知文案与命令参数
    name: str = ''
    #: 探测用地址（从本机视角出发，通常就是 127.0.0.1）
    host: str = '127.0.0.1'
    port: int = 25565
    #: auto  = 自动判定（端口中 server.properties 的 server-port 匹配者即为本实例负责的服务端）
    #: self  = 本 MCDR 实例管理的服务端（用 MCDR 的 start/stop 控制，玩家数用 MCDR 事件精确统计）
    #: command = 不由 MCDR 管理，由本实例用下面的命令启停（端口探测判断状态）
    #: watch = 只监视，不启停
    role: str = 'auto'
    #: 无人时是否允许自动休眠
    auto_sleep: bool = True
    #: 有人尝试连接时是否允许自动唤醒
    auto_wake: bool = True
    #: 常驻：**永不休眠**。适合"希望长期开着"的子服 —— 自动判定不会关它，
    #: 管理员手敲 `!!ig sleep`（含从别的实例发来的）也会被拒绝。
    #: 要临时关掉它，把这里改成 false 再 `!!ig reload`。
    always_on: bool = False
    #: 该子服所属 MCDR 实例的总线地址，如 "127.0.0.1:25600"；填了才能跨服通知/远程控制
    bus: Optional[str] = None
    #: role=command 时使用；启动命令（走 shell，支持 Windows 的 start.bat）
    start_command: Optional[str] = None
    #: role=command 时使用；休眠/停止命令（例如向服务端控制台发 stop，或调用脚本）
    stop_command: Optional[str] = None
    #: role=command 时使用；命令的工作目录
    working_directory: Optional[str] = None
    #: 覆盖全局的空闲阈值（秒）；留 null = 跟随顶层的 idle_timeout
    idle_timeout: Optional[float] = None


#: 假服务端监听的地址（写死，不做成选项）。真实服务端一般监听 0.0.0.0，
#: 假服务端跟着它走才是对的：任何能连到真实服务端的地址，休眠期间也该能连到它。
FAKE_SERVER_BIND = '0.0.0.0'

#: 总线口令的**历史默认值**。早期版本生成的配置里就是它；
#: 现在新生成的配置会自动填入随机串（见 new_bus_token），保留这个常量只为让自检能提醒老配置改掉。
LEGACY_BUS_TOKEN = 'idle_guard'

#: 示例配置里用的占位口令前缀（仓库里的 `user-config/idle_guard-config.json` 就是它）。
#: 照抄示例的多实例部署等于没有鉴权 —— 自检要能把它点名。
BUS_TOKEN_PLACEHOLDER_PREFIX = 'CHANGE_ME'


def new_bus_token() -> str:
    """生成一个新的总线口令（首次生成配置文件时自动填入）。

    为什么不留一个写死的默认口令：它是公开的 —— 一旦跨机器部署，谁都能拿它连上你的总线，
    替你对子服下达休眠/唤醒指令。所以默认给随机串；你想自定义也完全可以，
    改 `bus.token` 即可（**多个实例必须改成同一个值**，否则会互相报「鉴权失败」）。
    """
    return secrets.token_urlsafe(18)


def default_servers() -> List[ServerEntry]:
    """默认配置里带的示例子服。

    ⚠️ 这是**示例**，不是任何真实拓扑：某条若与这里逐字段相同，说明用户没改过它，
    `velocity` 同步时可以按 velocity.toml 改写或删除
    （见 `Supervisor._sync_servers_from_velocity`）。改这里的值等于改判定基准，别只为换例子而改。
    """
    return [
        ServerEntry(
            name='lobby', host='127.0.0.1', port=25565, role='watch',
            auto_sleep=False, bus='127.0.0.1:25600',
        ),
        ServerEntry(
            name='survival', host='127.0.0.1', port=25566, role='auto',
            auto_sleep=True, auto_wake=True, bus='127.0.0.1:25601',
        ),
    ]


@dataclass
class BusConfig(Serializable):
    """实例间通信总线配置。"""

    #: 本实例监听的地址，如 "127.0.0.1:25601"。
    #: 默认 "auto" = 照抄「本实例负责的那条子服」上写的 bus（见 servers[].bus）——
    #: 这样同一份配置可以直接复制到每个实例，不必为每台机器单独改这里。
    #: 留空（`null` / `""`）与 "auto" 等价：没设置就走自动规则
    #: （以前这里默认就是空的，老配置升级后因此能自动获得这个行为，不必手改）。
    bind: Optional[str] = 'auto'
    #: 通信口令，**所有实例必须一致**。首次生成配置时自动填入随机串，也可自行修改。
    token: str = field(default_factory=new_bus_token)
    #: 单次请求超时（秒）
    timeout: float = 3.0


@dataclass
class FakeServerConfig(Serializable):
    """假服务端配置（顶替休眠中的真实服务端端口）。

    这里只留一个开关；MOTD / 版本名 / 玩家看到的提示文字都在 lang/<语言>.json。
    监听地址固定 0.0.0.0（与真实服务端一致），不做成选项：它没有需要按机器区分的场景，
    留着只会多一行"要不要改"的疑问。
    """

    enabled: bool = True


@dataclass
class AnnounceConfig(Serializable):
    """通知行为开关（文案在 lang/<语言>.json）。"""

    enabled: bool = True
    #: 是否单独提醒"试图进入但被挡住"的玩家
    tell_attempt_players: bool = True
    #: 服务端开好之后，给那位被挡住的玩家放的音效（空字符串 = 不播放）
    #: 用原版音效 id 即可，例如 minecraft:block.note_block.pling（音符盒）、
    #: minecraft:block.note_block.bell（钟）、minecraft:entity.experience_orb.pickup（经验球）
    sound: str = 'minecraft:block.note_block.pling'


@dataclass
class VelocityConfig(Serializable):
    """从 Velocity 的 `velocity.toml` 同步子服清单。"""

    #: 是否启用这个同步
    enabled: bool = True
    #: `velocity.toml` 的路径。`auto` = 在 MCDR 工作目录附近按候选路径查找；也可填相对路径
    #: （相对 MCDR 工作目录）、绝对路径，支持 `~` 与环境变量。
    #: 读到可用的 `[servers]` 时它是**子服清单的权威来源**：缺的子服补进来，
    #: 默认配置里没改过的示例条目按它改写或删除；用户改过的条目一律不动（只提醒地址对不上）。
    path: str = 'auto'


@dataclass
class Config(Serializable):
    #: 本实例名（用于日志与总线），auto = 用 role=self 的服务器名
    instance_name: str = 'auto'
    #: 界面语言：auto = 跟随 MCDR 的 language 设置；也可写 zh_cn / en_us 等
    #: （可用语言见插件包内的 lang/ 目录，缺省文案见 lang/en_us.json）
    language: str = 'auto'
    #: 侦察间隔（秒）。默认 10 分钟：空闲判定按小时级阈值，没必要查得太勤
    check_interval: float = 600.0
    #: 状态心跳间隔（秒）。只针对**本实例负责**的子服做一次短超时的状态查询，
    #: 让"刚关 / 刚开完"的状态在几秒内就反映到 !ig status 和命令回复上
    #: （否则要等一个侦察周期，默认 10 分钟 —— 线上反馈的"关完还显示运行中、
    #: 开好了还显示启动中"就是这个）。
    #: 设为 0（或负数）= 关掉心跳，退回"只有侦察周期才更新状态"。
    state_tick: float = 5.0
    #: 全局空闲阈值：连续无人多少秒后休眠
    idle_timeout: float = 3600.0
    #: 启动保护期：服务端启动完成后这段时间内不判定空闲（秒），避免"刚开就关"。
    #: 被唤醒的场景也由它保护（唤醒后服务端就绪 → 起算），所以不再单独设"唤醒保护期"
    min_uptime: float = 300.0
    #: 等待服务端启动完成的超时（秒）
    startup_timeout: float = 300.0
    #: 等待服务端停止完成的超时（秒）
    stop_timeout: float = 180.0
    #: 等待端口从"被占用"变为"可绑定"的超时（秒）
    port_release_timeout: float = 60.0
    #: 两次唤醒之间的最小间隔（秒），避免玩家连续尝试导致重复启动
    wake_cooldown: float = 15.0
    #: 首次唤醒时用于提示的预计启动耗时（秒）；之后会用实测值
    fallback_startup_seconds: float = 60.0
    #: "热启动"的判定窗口（秒）：服务端停止后**这么快之内**又被唤醒的，算热启动
    #: （世界数据多半还在系统缓存里、起得更快）。ETA 会按冷 / 热分别统计。
    hot_startup_window: float = 300.0
    #: 记住"谁尝试进入过"的时长（秒），用于服务端起来后单独提醒
    player_attempt_window: float = 600.0
    #: 统计玩家数时忽略的玩家名（正则，例如假人：["^bot_", "^Bot"]）
    ignore_players: List[str] = field(default_factory=list)
    #: 连续启动失败多少次后锁定自动唤醒
    max_wake_failures: int = 3
    #: `!!ig sleep` 发出后，等多久之内可以用 `!!ig confirm` 确认（秒）
    confirm_timeout: float = 30.0
    velocity: VelocityConfig = field(default_factory=VelocityConfig)
    #: 服务端输出里命中这些正则时，认为它"正在忙"（备份 / 保存），这期间**不做自动休眠**。
    #: 只拦自动休眠：管理员手敲的 `!!ig sleep` 照常执行（你明确要关，插件不拦）。
    #: 默认覆盖常见的备份插件文案；不需要就把列表清空成 `[]`。
    busy_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_BUSY_PATTERNS))
    #: 命中一次"忙"之后，往后这么多秒内不做自动休眠（秒）。到点自动失效，
    #: 所以不需要服务端再打印一条"备份结束"（那种日志各家写法都不一样、靠不住）。
    busy_hold_seconds: float = 300.0
    #: 打印详细调试日志
    debug: bool = False

    bus: BusConfig = field(default_factory=BusConfig)
    fake_server: FakeServerConfig = field(default_factory=FakeServerConfig)
    announce: AnnounceConfig = field(default_factory=AnnounceConfig)

    #: 子服务器列表，默认是一份可直接改用的示例（见 `default_servers`）：
    servers: List[ServerEntry] = field(default_factory=default_servers)


    # ------------------------------------------------------------------ #
    # 文件布局：**只在写进 config.json / 从 config.json 读时**按功能分组
    # ------------------------------------------------------------------ #
    # 内部字段一律平铺（`config.idle_timeout`），一是代码与测试都直接读属性，
    # 二是这样"老配置（平铺）"与"新配置（分组）"能同时读 —— 不必写迁移、不会丢值。

    def serialize(self) -> dict:
        """写文件时按 `CONFIG_LAYOUT` 分组（只影响文件长相，属性访问不变）。"""
        return _group_layout(super().serialize())

    @classmethod
    def deserialize(cls, data, **kwargs):
        """读文件时先把分组摊平并迁移旧键：分组的、平铺的老配置都能读。"""
        return super().deserialize(_migrate_legacy_velocity(_flatten_layout(data)), **kwargs)

#: role 的合法取值
VALID_ROLES = ('auto', 'self', 'command', 'watch')


# --------------------------------------------------------------------------- #
# 数值字段的范围
# --------------------------------------------------------------------------- #

#: 数值配置项的合法范围：(最小, 最大, 默认值)。超范围的值会被**改回默认值**并记一条 warning。
#:
#: 为什么必须有这层：这些值全都会被直接喂给 `Event.wait()` / `time.sleep()` / 超时比较，
#: 而 `Config.deserialize` 对它们不做任何校验。实测过的两种后果都很隐蔽：
#: - `state_tick=0` + `check_interval=0`：1 秒内跑 135 万次 `poll_once`（每轮都要查所有子服），
#:   把 CPU 烧满；
#: - `check_interval=1e400`（= inf）：`Event.wait(inf)` 抛 **OverflowError**，
#:   轮询线程直接死掉 —— 自动休眠、唤醒、假服务端维护全部停止，而 MCDR 日志里一句都没有。
#:
#: 上界统一取一天（86400），"超大"与 inf / nan 一样按非法处理：想做小时级的间隔，
#: 顶到一天已经绰绰有余，再大只会让状态表变成假的。
#: `state_tick` 的合法区间包含 0 与负数：那是文档里明确支持的"关掉心跳"写法。
NUMERIC_LIMITS: Dict[str, Tuple[float, float, float]] = {
    'check_interval': (0.1, 86400.0, 600.0),
    'state_tick': (-86400.0, 86400.0, 5.0),
    # 下限刻意取 0.05 而不是 1：亚秒级的阈值在测试与"快速验证配置"时是真会用到的
    # （仓库自己的集成测试就用 0.2）。要挡的是 0 / 负数 / inf / nan / 超大 —— 0 会让
    # `idle < timeout` 恒不成立，等于把自动休眠关掉却又不告诉任何人。
    'idle_timeout': (0.05, 2592000.0, 3600.0),
    'min_uptime': (0.0, 2592000.0, 300.0),
    'startup_timeout': (1.0, 86400.0, 300.0),
    'stop_timeout': (1.0, 86400.0, 180.0),
    'port_release_timeout': (0.5, 86400.0, 60.0),
    'wake_cooldown': (0.0, 86400.0, 15.0),
    'fallback_startup_seconds': (0.1, 86400.0, 60.0),
    'hot_startup_window': (0.0, 2592000.0, 300.0),
    'player_attempt_window': (0.0, 2592000.0, 600.0),
    'confirm_timeout': (5.0, 86400.0, 30.0),
    'busy_hold_seconds': (0.0, 2592000.0, 300.0),
    'bus.timeout': (0.1, 3600.0, 3.0),
    # 次数类：非整数、0、负数、超大值都没有意义
    'max_wake_failures': (1, 1000, 3),
}


def _valid_number(value, low: float, high: float) -> bool:
    """值是不是"范围内的有限数"；bool 与字符串一律算非法（`True` 是 int 的子类）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value or value in (float('inf'), float('-inf')):    # nan / inf
        return False
    return low <= value <= high


def sanitize_numbers(config) -> List[Tuple[str, object, object]]:
    """把越界 / 非有限的数值配置改回默认值，返回 [(字段, 原值, 默认值)]。

    就地修改 `config`（含 `config.bus.timeout` 这种子对象字段）。调用方负责记日志。
    """
    changed: List[Tuple[str, object, object]] = []
    for name, (low, high, default) in NUMERIC_LIMITS.items():
        owner, _, field = name.rpartition('.')
        target = getattr(config, owner) if owner else config
        if target is None:
            continue
        current = getattr(target, field, None)
        if _valid_number(current, low, high):
            continue
        setattr(target, field, default)
        changed.append((name, current, default))
    return changed


# --------------------------------------------------------------------------- #
# 配置文件的分组布局
# --------------------------------------------------------------------------- #

#: 文件里按功能分组：「一眼找到要改的那一项」比少两行更重要。
#: 键 = 组名，值 = 组内字段。组名与已有的子对象同名（如 `announce`）时表示"塞进那个子对象里"。
CONFIG_LAYOUT: Dict[str, Tuple[str, ...]] = {
    # 什么时候休眠 / 空闲怎么判定
    'sleep': (
        'idle_timeout', 'min_uptime', 'check_interval', 'state_tick', 'wake_cooldown',
        'confirm_timeout', 'busy_patterns', 'busy_hold_seconds', 'ignore_players',
    ),
    # 起停一台服务端要等多久
    'startup': (
        'startup_timeout', 'stop_timeout', 'port_release_timeout',
        'fallback_startup_seconds', 'hot_startup_window', 'max_wake_failures',
    ),
    # 通知相关（这个组本身就是一个子对象，多放进去的字段会在读的时候摊回顶层）
    'announce': ('player_attempt_window',),
}

#: 生成文件时的字段顺序（没有列到的字段会按原顺序补在后面）
CONFIG_FILE_ORDER: Tuple[str, ...] = (
    'instance_name', 'language', 'debug',
    'sleep', 'startup', 'announce', 'fake_server', 'bus',
    'velocity', 'servers',
)


def _migrate_legacy_velocity(data):
    """旧配置里那个顶层 `velocity_config` 字符串 → 新的 `velocity` 分组。

    旧写法一个字段三种含义（`auto` 查找 / `off` 关闭 / 其余当路径），新写法拆成
    `velocity.enabled` + `velocity.path`。老配置照旧能读：关闭值转成"关闭"，
    其余非空值转成路径；已经用新键的配置不受影响。
    """
    if not isinstance(data, dict) or 'velocity_config' not in data:
        return data
    legacy = str(data.pop('velocity_config') or '').strip()
    bucket = data.get('velocity') if isinstance(data.get('velocity'), dict) else {}
    if legacy.lower() in VELOCITY_OFF_VALUES:
        bucket.setdefault('enabled', False)
    elif legacy:
        bucket.setdefault('path', legacy)
    if bucket:
        data['velocity'] = bucket
    return data


def _group_layout(flat: dict) -> dict:
    """平铺字段 → 分组后的文件内容。"""
    out: dict = {}
    placed: set = set()
    for key in CONFIG_FILE_ORDER:
        names = CONFIG_LAYOUT.get(key)
        if names is None:
            if key in flat:
                out[key] = flat[key]
                placed.add(key)
            continue
        bucket = {name: flat[name] for name in names if name in flat}
        existing = flat.get(key)           # 例如 announce 这个真子对象
        if isinstance(existing, dict):
            merged = dict(existing)
            merged.update(bucket)
            bucket = merged
        if bucket:
            out[key] = bucket
        placed.add(key)
        placed.update(names)
    for key, value in flat.items():        # 兜底：以后新增的字段也不会被这里弄丢
        if key not in placed:
            out[key] = value
    return out


def _flatten_layout(data) -> dict:
    """分组后的文件内容 → 平铺字段（老配置本来就是平铺的，原样通过）。"""
    if not isinstance(data, dict):
        return data
    flat = dict(data)
    for group, names in CONFIG_LAYOUT.items():
        bucket = flat.get(group)
        if not isinstance(bucket, dict):
            continue
        leftover = {key: value for key, value in bucket.items() if key not in names}
        for name in names:
            # 平铺写法优先：两个地方都写了的话，以顶层那个为准（它更显眼）
            if name in bucket and name not in data:
                flat[name] = bucket[name]
        if leftover:
            flat[group] = leftover
        else:
            flat.pop(group, None)
    return flat
