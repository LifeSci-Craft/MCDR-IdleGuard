"""数据模型与常量：状态枚举、子服运行时对象、展示快照、二次确认记录。

从 `supervisor.py` 里拆出来的：那个文件原本 2200 多行，状态机、总线、假服务端、
数据模型全挤在一起，改一处要翻半天。这里**只放数据结构**，行为留在各处服务对象里。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .config import ServerEntry
from .fakeserver import FakeServer
from .mcproto import PingResult


class State(str, Enum):
    UNKNOWN = 'unknown'
    OFFLINE = 'offline'      # 服务端未运行，且端口无人接管（过渡态：插件随时会挂上假服务端）
    SLEEPING = 'sleeping'    # 已休眠：服务端停止 + 假服务端顶替端口
    STOPPED = 'stopped'      # 被手动/外部停止：服务端未运行，且**插件不接管端口**
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'


MANAGED_ROLES = ('self', 'command')

#: "MCDR 漏报离开"的自愈门槛（秒）：状态查询必须**连续**这么久都报 0 人在线，
#: 才允许拿它去清掉事件里那个名字。留这么长的窗口是为了不动摇"两个信号矛盾时先不动"
#: 这条安全线 —— 真实服务端不可能一边有人玩、一边持续一分钟说自己是空的。
STALE_PLAYER_SECONDS = 60.0

#: 一个启动耗时样本：(本次耗时秒数, 距上次停止的秒数 或 None)
StartupSample = Tuple[float, Optional[float]]


def _parse_startup_sample(item) -> Optional[StartupSample]:
    """读一个样本，兼容三种历史写法：

    - 新格式 `[11.2, 45.0]`：耗时 + 停机时长；
    - 旧格式 `11.2`（单值）或 `[11.2, 12.0]`（一串耗时）：停机时长未知，记 None。
    """
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return (float(item), None) if item > 0 else None
    if isinstance(item, (list, tuple)) and item:
        seconds = item[0]
        if not (isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and seconds > 0):
            return None
        if len(item) > 1 and isinstance(item[1], (int, float)) \
                and not isinstance(item[1], bool) and item[1] >= 0:
            return (float(seconds), float(item[1]))
        return (float(seconds), None)      # 旧格式的纯列表：只有耗时
    return None

#: 合法的 Minecraft 玩家名。名字来自客户端握手（是可以随便造的），
#: 而这个名字会被拼进 tellraw / playsound 这类命令里执行 —— 必须过滤，
#: 否则一个叫 `a] ... ` 的假客户端就能往服务端注入命令。
_PLAYER_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,16}$')


def safe_player_name(player: Optional[str]) -> str:
    """只放行合法玩家名；不合法一律返回空串（调用方据此跳过）。"""
    name = str(player or '').strip()
    return name if _PLAYER_NAME_RE.match(name) else ''


class ManagedServer:
    """一个由本实例负责启停的子服务器。"""

    def __init__(self, entry: ServerEntry, role: str):
        self.entry = entry
        self.role = role
        self.state = State.UNKNOWN
        self.state_since = time.monotonic()
        self.detail = ''

        self.empty_since: Optional[float] = None
        self.locked = False
        self.wake_failures = 0
        self.last_wake_ts = 0.0
        self.launch_started: Optional[float] = None
        self.started_at: Optional[float] = None
        #: 上一次"服务端停下来"的时刻（monotonic）：用来算"停机多久后又被唤醒"，
        #: 冷 / 热启动的 ETA 靠它分桶。None = 这次会话里还没停过。
        #: ⚠️ 哨兵值故意不用 0.0 —— 那会和真实时刻共用一条数轴：
        #: monotonic 是"开机以来的秒数"，在刚启动的容器里它可能还不到 7200，
        #: `monotonic() - 7200` 就成了负数，于是被判成"没停过"。
        self.last_stopped_at: Optional[float] = None
        self.last_startup_seconds: Optional[float] = None

        self.players: set = set()
        #: 最近一次**成功**的状态查询时刻（monotonic）：只有它比"最后确认有人"更晚，
        #: 才允许用查询结果去纠正 MCDR 漏报的"离开"（见 _drop_stale_players）
        self.slp_at = 0.0
        #: 连续报"0 人在线"是从什么时候开始的（None = 最近一次查询不是 0 人 / 查不通）
        self.slp_zero_since: Optional[float] = None
        #: 最近一次确认"这服上确实有人在"的时刻（进服事件 / 出现在状态查询的名单里）
        self.players_seen_at = 0.0
        self.slp = PingResult()
        self.fake: Optional[FakeServer] = None
        #: 这台服是被**外部**停掉的（后台敲 `stop`、别的程序关的、崩了之后被关的），
        #: 而不是插件自己休眠 —— 这种"已停止"插件不接管端口、也不会替玩家自动开服。
        #: 服务端重新起来、或插件明确要休眠/唤醒时清掉。
        self.external_stop = False
        self.attempts: Dict[str, float] = {}
        #: 唤醒完成时那位玩家还不在线（他多半已经被假服务端挡回客户端了），
        #: 于是记下"等他进服要补一句『服开好了』"：player -> 到期时刻（monotonic）。
        #: 之所以要这样：唤醒是在**服务端内部**广播的，而他此刻不在这个服务端里，
        #: 单独 tell 也发不出去（玩家不在线）—— 结果就是他什么提示都收不到。
        self.pending_greetings: Dict[str, float] = {}

        self.action_lock = threading.RLock()
        self.startup_event = threading.Event()
        self.stop_event = threading.Event()
        #: "服务端正在忙"的截止时刻（monotonic）：命中 `busy_patterns` 时设置，
        #: 期间**不做自动休眠**（备份跑到一半被关掉会毁掉备份）。0 = 不忙
        self.busy_until = 0.0
        #: 触发忙碌判定的那行服务端输出（给 diag 展示，好核对是不是误报）
        self.busy_reason = ''
        #: 本实例是否正在执行休眠/唤醒动作（动作期间状态由动作线程负责，轮询不去改它）
        self.action_in_progress = False
        #: 是否已经就"用状态查询兜底判就绪"记过一次日志
        self.ready_fallback_logged = False
        #: 假服务端接管端口的后台重试线程（端口迟迟不释放时不让玩家一直撞被拒）
        self.takeover_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def idle_timeout(self) -> float:
        return self.entry.idle_timeout if self.entry.idle_timeout else 0.0

    def record_attempt(self, player: Optional[str], now: float, window: float) -> None:
        key = player or '?'
        self.attempts[key] = now
        for stale in [k for k, ts in self.attempts.items() if now - ts > window]:
            self.attempts.pop(stale, None)

    def attempt_players(self, now: float, window: float) -> List[str]:
        return [name for name, ts in self.attempts.items()
                if name != '?' and now - ts <= window]


@dataclass
class ServerView:
    """用于展示/总线传输的服务器状态快照。"""

    name: str = ''
    host: str = ''
    port: int = 0
    role: str = 'watch'
    state: str = State.UNKNOWN.value
    players: int = -1
    players_max: int = -1
    idle_seconds: float = -1.0
    idle_timeout: float = 0.0
    locked: bool = False
    #: 这台子服是否参与自动休眠（浮窗里要说明"空闲但不关"的原因）
    auto_sleep: bool = True
    #: 常驻：永不休眠（自动与手动都拒绝）
    always_on: bool = False
    #: 管它的那个实例现在还能联系上吗。
    #: 只对"别人负责的服"有意义：联系不上时状态表会标出来（`@实例名(离线)`），
    #: 否则管理员分不清「没人管它」和「管它的实例掉线了」—— 这两种在旧版里长得一模一样。
    owner_online: bool = True
    detail: str = ''
    instance: str = ''
    local: bool = False

    def to_dict(self) -> dict:
        return {
            'name': self.name, 'host': self.host, 'port': self.port, 'role': self.role,
            'state': self.state, 'players': self.players, 'players_max': self.players_max,
            'idle_seconds': self.idle_seconds, 'idle_timeout': self.idle_timeout,
            'locked': self.locked, 'auto_sleep': self.auto_sleep, 'detail': self.detail,
            'always_on': self.always_on,
        }

    @classmethod
    def from_dict(cls, data: dict, instance: str = '') -> 'ServerView':
        view = cls()
        for key in ('name', 'host', 'role', 'state', 'detail'):
            if isinstance(data.get(key), str):
                setattr(view, key, data[key])
        for key in ('port', 'players', 'players_max'):
            if isinstance(data.get(key), (int, float)):
                setattr(view, key, int(data[key]))
        for key in ('idle_seconds', 'idle_timeout'):
            if isinstance(data.get(key), (int, float)):
                setattr(view, key, float(data[key]))
        view.locked = bool(data.get('locked', False))
        view.auto_sleep = bool(data.get('auto_sleep', True))
        view.always_on = bool(data.get('always_on', False))
        # 对端发来的字段不能全信（可能是旧版本、也可能被人改过）：枚举值不合法就降级。
        # 以前 role/state 原样收下 —— 一个未知 state 会直接进状态表与 _precheck_action 的判断。
        if view.role not in ('self', 'command', 'watch'):
            view.role = 'watch'
        if view.state not in {item.value for item in State}:
            view.state = State.UNKNOWN.value
        view.instance = instance
        return view


@dataclass
class PendingAction:
    """等待管理员二次确认的动作（目前只有 sleep —— 关服是不可逆的，值得多问一句）。"""

    action: str
    server: str
    by: str
    deadline: float


