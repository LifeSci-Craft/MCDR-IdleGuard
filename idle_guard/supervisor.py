"""
调度核心：侦察玩家数、空闲休眠、按需唤醒、跨服通知

状态机（每个由本实例负责的子服一个 ManagedServer）：

        启动完成                    空闲超时 / !!ig sleep
  STARTING ──► RUNNING ──────────────────────► STOPPING ──► SLEEPING
      ▲            ▲                                          │
      │            └────────── 假服务端收到连接尝试 ◄──────────┘
      │                          （或 !!ig wake）
      └── 启动失败 / 超时 ──────────────────────────────────► SLEEPING（回退，等下一次尝试）

- RUNNING：真实服务端在跑；用 MCDR 事件精确统计本服玩家（role=self）
- SLEEPING：真实服务端已停；假服务端顶替端口，负责"劝退 + 触发唤醒"
- 空闲判定采用双信号（MCDR 事件 + 状态查询），两者不一致时宁可不休眠
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from mcdreforged.api.types import PluginServerInterface

from . import i18n
from . import velocity as velocity_module
from .bus import Bus
from .config import Config, ServerEntry
from .fakeserver import FakeServer
from .mcproto import PingResult, can_bind, port_open, status_ping


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

#: 实测启动耗时的落盘文件名（放在 MCDR 给本插件的 data 目录，即 config/idle_guard/）
#: 有了它，插件重载/重启之后给玩家报的"预计 xx 秒"仍然基于这台服的真实历史
_STARTUP_STATS_FILE = 'startup_seconds.json'

#: 每个子服最多留多少个启动耗时样本（ETA 取这些样本的中位数）
_STARTUP_SAMPLES_MAX = 8


def _median(values: List[float]) -> float:
    """中位数（不动入参）。"""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0

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
        self.warned = False
        self.locked = False
        self.wake_failures = 0
        self.last_wake_ts = 0.0
        self.launch_started: Optional[float] = None
        self.started_at: Optional[float] = None
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
        view.instance = instance
        return view


@dataclass
class PendingAction:
    """等待管理员二次确认的动作（目前只有 sleep —— 关服是不可逆的，值得多问一句）。"""

    action: str
    server: str
    by: str
    deadline: float


class Supervisor:
    def __init__(self, mcdr: PluginServerInterface, config: Config, plugin_id: str = 'idle_guard',
                 plugin_version: str = ''):
        self._mcdr = mcdr
        self._logger = mcdr.logger
        self._config = config
        self.plugin_id = plugin_id
        #: 顶栏要印的版本号（由插件入口传进来，避免 commands 反向导入包）
        self.plugin_version = plugin_version
        self._language = self._resolve_language()
        self._stopping = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        self._managed: Dict[str, ManagedServer] = {}
        self._views: Dict[str, ServerView] = {}      # 所有子服（含只监视的）最新快照
        self._last_ping: Dict[str, PingResult] = {}
        self._self_name: Optional[str] = None
        self.instance_name = ''
        #: 发起人 -> 待二次确认的动作（每个管理员各存一份，避免互相顶掉）
        self._pending: Dict[str, PendingAction] = {}
        self._pending_lock = threading.Lock()
        #: 各子服最近的实测启动耗时（秒）列表；内存 + 落盘，ETA 取它的中位数
        self._data_dir = self._resolve_data_dir()
        self._startup_seconds: Dict[str, List[float]] = self._load_startup_seconds()

        self._ignore_patterns: List[re.Pattern] = []
        for pattern in config.ignore_players:
            try:
                self._ignore_patterns.append(re.compile(pattern))
            except re.error as exc:
                self._logger.warning(f'ignore_players 中的正则 {pattern!r} 无效：{exc}')

        # 先按 velocity.toml 补齐子服清单，再做角色判定（判 role=auto 要靠这些条目）
        self._sync_servers_from_velocity()
        self._compile_config()
        self._bus_bind = self._resolve_bus_bind()
        self._bus = Bus(
            bind_address=self._bus_bind,
            token=config.bus.token,
            handler=self._on_bus_message,
            logger=self._logger,
            timeout=config.bus.timeout,
        )

    # ================================================================== 多语言

    def _resolve_language(self) -> str:
        """解析出本次运行使用的语言（config.language = auto 时跟随 MCDR）。"""
        mcdr_language = None
        getter = getattr(self._mcdr, 'get_mcdr_language', None)
        if callable(getter):
            try:
                mcdr_language = getter()
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(f'读取 MCDR 语言设置失败：{type(exc).__name__}: {exc}')
        language, warning = i18n.resolve(self._config.language, mcdr_language)
        for error in i18n.pop_errors():
            self._logger.warning(i18n.translate('language.file_broken', language, detail=error))
        if warning:
            self._logger.warning(warning)
        if self._config.language != 'auto':
            self._logger.debug(f'界面语言：{language}（配置指定）')
        else:
            self._logger.debug(f'界面语言：{language}（跟随 MCDR 的 language 设置）')
        return language

    def tr(self, key: str, **kwargs) -> str:
        """按当前语言取一条文案。命令层、通知文案、提示都用它。"""
        return i18n.translate(key, self._language, **kwargs)

    @property
    def language(self) -> str:
        return self._language

    def state_text(self, state) -> str:
        value = state.value if isinstance(state, State) else str(state)
        return self.tr(f'state.{value}')

    def role_text(self, role: str) -> str:
        return self.tr(f'role.{role}')

    # ================================================================== 生命周期

    def start(self) -> None:
        self._bus.start()
        for managed in self._managed.values():
            self._ensure_fake_server(managed, retry=True)
        self._poll_thread = threading.Thread(target=self._poll_loop, name='IdleGuard-Poll', daemon=True)
        self._poll_thread.start()
        names = ', '.join(self._managed) or '无'
        self._logger.info(f'空闲守卫已启动：实例 {self.instance_name}，负责 {names}')
        if not self._managed:
            self._logger.warning(
                '没有任何子服被判定为「本实例负责」（role=self/command 都没能匹配上）。'
                '本实例目前只会监视玩家数，不会休眠任何服务端。'
            )

    def shutdown(self) -> None:
        self._stopping.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._poll_thread = None
        for managed in self._managed.values():
            if managed.fake is not None:
                managed.fake.stop()
                managed.fake = None
        self._bus.stop()

    def _resolve_bus_bind(self) -> Optional[str]:
        """解析 bus.bind：填 "auto" 时取「本实例负责的子服条目」上写的总线地址，
        这样同一份配置可以直接复制到每个实例，不需要为每台机器改一个端口。"""
        bind = self._config.bus.bind
        if not (isinstance(bind, str) and bind.strip().lower() == 'auto'):
            return bind
        for managed in self._managed.values():
            if managed.role == 'self' and managed.entry.bus:
                self._logger.info(f'bus.bind 为 auto，按本实例负责的 {managed.name} 取其总线地址 {managed.entry.bus}')
                return managed.entry.bus
        self._logger.warning('bus.bind 为 auto，但没找到「本实例负责」的子服条目（或该条目未填 bus），总线不会启动')
        return None

    # ================================================================== 配置解析

    def _sync_servers_from_velocity(self) -> None:
        """按 `velocity.toml` 的 `[servers]` 段补齐子服清单（**只补缺的**）。

        为什么只补不改：`servers[]` 里那些 `role` / `auto_sleep` / `bus` 是管理员按本机情况
        手写的，自动同步不该动它们；地址对不上时只提醒一句，不覆盖。
        """
        setting = (self._config.velocity_config or '').strip()
        if setting.lower() in velocity_module.OFF_VALUES:
            return
        try:
            path, found = velocity_module.load_servers(setting, os.getcwd())
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(f'读取 Velocity 配置出错（跳过同步）：{type(exc).__name__}: {exc}')
            return
        if path is None:
            self._logger.debug(f'没找到 velocity.toml（velocity_config={setting!r}），跳过子服同步')
            return
        if not found:
            self._logger.info(f'读到了 {path}，但里面没有可用的 [servers] 条目，跳过同步')
            return

        known = {entry.name: entry for entry in self._config.servers}
        added: List[str] = []
        for name, (host, port) in sorted(found.items()):
            entry = known.get(name)
            if entry is None:
                self._config.servers.append(ServerEntry(name=name, host=host, port=port))
                added.append(name)
            elif entry.port != port or entry.host != host:
                self._logger.warning(
                    f'配置里 {name} 写的是 {entry.host}:{entry.port}，'
                    f'而 {os.path.basename(path)} 里是 {host}:{port}；保留你的配置，'
                    f'想跟着 Velocity 走就改掉这一条'
                )
        if not added:
            self._logger.debug(f'velocity.toml 里的 {len(found)} 个子服都已在配置中')
            return
        self._logger.info(f'已从 {path} 补进 {len(added)} 个子服：{", ".join(added)}')
        self._save_config()

    def _save_config(self) -> None:
        """把当前配置写回 config.json（自动补过子服时用，好让管理员看得见改动）。"""
        if self._data_dir is None:
            return
        path = os.path.join(self._data_dir, 'config.json')
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(self._config.serialize(), handle, ensure_ascii=False, indent=4)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(f'配置写回失败（不影响本次运行）：{type(exc).__name__}: {exc}')

    def _compile_config(self) -> None:
        """把配置解析成运行时结构：判定 role、建立 ManagedServer、确定实例名。"""
        entries = list(self._config.servers)
        self_name = self._resolve_self(entries)
        self._self_name = self_name

        # 端口匹配上了"我的服务端"，但条目 role 写成了 watch —— 这会让本实例谁都不管，
        # 用户只会看到「!!ig sleep xxx 无效」。这里把原因讲清楚（这是真实踩过的坑）。
        # 注意 role=auto 不属于这种情况：下面会把它解析成 self（自己的服务端就该自己管）。
        matched = next((e for e in entries if e.name == self_name), None) if self_name else None
        if matched is not None and matched.role == 'watch':
            self._logger.warning(
                f'提示：本实例的服务端是 {matched.name}（server-port={matched.port}），'
                f'但配置里它的 role="watch"，因此本实例只会监视它、不会启停它。'
                f'若希望由本实例管理，请把该条目的 role 改成 "self"（或直接写 "auto"）'
            )

        for entry in entries:
            role = entry.role if entry.role in ('self', 'command', 'watch') else 'auto'
            if entry.role not in ('auto', 'self', 'command', 'watch'):
                self._logger.warning(f'服务器 {entry.name} 的 role={entry.role!r} 非法，按 auto 处理')
            if role == 'auto':
                role = 'self' if entry.name == self_name else 'watch'
            if role in MANAGED_ROLES:
                if entry.role == 'command' and not entry.start_command:
                    self._logger.warning(f'服务器 {entry.name} 是 role=command 但没写 start_command，只能监视')
                    role = 'watch'
                self._managed[entry.name] = ManagedServer(entry, role)
                managed = self._managed[entry.name]
                if managed.entry.idle_timeout is None:
                    managed.entry.idle_timeout = self._config.idle_timeout
            else:
                if entry.role == 'self':
                    self._logger.warning(f'服务器 {entry.name} 声明为 role=self 但不是本实例管理的那个')
        if self_name:
            self._logger.info(f'本实例负责的子服：{self_name}')
        self.instance_name = (
            self_name if self._config.instance_name == 'auto' or not self._config.instance_name
            else self._config.instance_name
        )

    def _resolve_self(self, entries: List[ServerEntry]) -> Optional[str]:
        explicit = [e for e in entries if e.role == 'self']
        if len(explicit) == 1:
            return explicit[0].name
        if len(explicit) > 1:
            self._logger.warning('配置里有多个 role=self，将按 server.properties 的端口来判定')

        own_port = self._read_own_server_port()
        if own_port is not None:
            matched = [e for e in entries if e.port == own_port]
            if len(matched) == 1:
                self._logger.info(f'按 server.properties 的 server-port={own_port} 判定本实例负责 {matched[0].name}')
                return matched[0].name
            if len(matched) > 1:
                self._logger.warning(f'有多个子服都填了端口 {own_port}，无法自动判定，请显式指定 role=self')
                return explicit[0].name if explicit else None
            self._logger.warning(f'没有任何子服条目匹配 server.properties 的 server-port={own_port}，'
                                 f'请检查配置中的端口')
        return explicit[0].name if explicit else None

    def _read_own_server_port(self) -> Optional[int]:
        """从 MCDR 工作目录下的 server.properties 里读出本服务端的端口。"""
        candidates: List[str] = []
        try:
            mcdr_config = self._mcdr.get_mcdr_config()
            working_directory = mcdr_config.get('working_directory')
            if working_directory:
                candidates.append(os.path.join(working_directory, 'server.properties'))
        except Exception:  # noqa: BLE001 - 取不到配置不是致命错误
            pass
        candidates.extend([
            os.path.join('server', 'server.properties'),
            'server.properties',
        ])
        for path in candidates:
            try:
                if not os.path.isfile(path):
                    continue
                with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                    for line in handle:
                        line = line.strip()
                        if line.startswith('server-port') and '=' in line:
                            value = line.split('=', 1)[1].strip()
                            if value.isdigit():
                                return int(value)
            except OSError:
                continue
        return None

    # ================================================================== 轮询主循环

    def _poll_loop(self) -> None:
        """一个线程跑两个节拍：

        - **侦察**（每 check_interval 秒）：查所有子服的协议状态、维护假服务端、判定空闲；
        - **状态心跳**（每 state_tick 秒）：只对**本实例负责**的子服做一次短超时状态查询，
          把状态刷新到秒级。

        为什么要拆开：状态原先只在侦察里算，于是"服务端刚关掉"要等到下一个侦察周期
        （默认 10 分钟）才反映出来 —— 线上反馈就是"关了还显示运行中、开好了还显示启动中"。
        心跳只碰自己负责的那一两台，成本很低，不会因为别的子服连不上而变慢。
        """
        # 启动后先等一会，避开 MCDR/服务端自身的启动阶段
        if self._stopping.wait(2.0):
            return
        tick = self._config.state_tick
        # ≤0 = 关掉心跳（退回"只有侦察周期才更新状态"）；正数则至少 0.2 秒，别把 CPU 烧了
        interval = max(0.2, tick) if tick > 0 else self._config.check_interval
        next_full = 0.0                      # 立刻先做一次完整侦察
        while not self._stopping.is_set():
            try:
                if time.monotonic() >= next_full:
                    self.poll_once()
                    next_full = time.monotonic() + self._config.check_interval
                else:
                    self._tick_states()
            except Exception as exc:  # noqa: BLE001 - 轮询线程必须活着
                self._logger.error(f'轮询时出现异常：{type(exc).__name__}: {exc}', exc_info=True)
            if self._stopping.wait(interval):
                return

    def _tick_states(self) -> None:
        """状态心跳：把每一行子服的状态都刷到秒级。

        - 本实例负责的：短超时状态查询 + 状态机 + 空闲计时；
        - 只监视的（别的实例负责 / 没人负责）：也做一次短超时查询刷新缓存快照。

        为什么连"只监视"的也要刷：状态表上那一行在没有可信来源时用的是这份缓存快照，
        而缓存原先只在侦察周期里更新 —— 对方实例挂了/那台服被外部关掉之后，
        状态表会拿着几分钟前的旧数据说"运行中"（线上反馈之一）。
        """
        managed = list(self._managed.values())
        watched = [e for e in self._config.servers if e.name and e.name not in self._managed]
        targets = [m.entry for m in managed] + watched
        if not targets:
            return

        results: Dict[str, PingResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = {pool.submit(status_ping, e.host, e.port, 1.0): e for e in targets}
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                try:
                    results[entry.name] = future.result()
                except Exception as exc:  # noqa: BLE001
                    self._logger.debug(f'心跳查询 {entry.name} 失败：{type(exc).__name__}: {exc}')

        for managed_server in managed:
            ping = results.get(managed_server.name)
            if ping is not None:
                self._last_ping[managed_server.name] = ping
            self._update_managed(managed_server, ping)
            if managed_server.role == 'self':
                # 本实例负责：玩家数来自 MCDR 的进出事件，永远是新鲜的，所以空闲计时
                # 也可以在这里跟着刷新（role=command 的人数靠状态查询，交给侦察就好）
                self._check_idle(managed_server)
        for entry in watched:
            ping = results.get(entry.name)
            if ping is not None:
                self._last_ping[entry.name] = ping
            self._views[entry.name] = self._build_view(entry, ping)
        for managed_server in managed:
            self._ensure_fake_server(managed_server, retry=True)

    def poll_once(self) -> None:
        """侦察一轮：更新所有子服状态、维护假服务端、判定空闲。"""
        results = self._ping_all()
        self._last_ping = results
        for entry in self._config.servers:
            self._views[entry.name] = self._build_view(entry, results.get(entry.name))

        for managed in self._managed.values():
            self._update_managed(managed, results.get(managed.name))
        for managed in self._managed.values():
            self._ensure_fake_server(managed, retry=True)
        for managed in self._managed.values():
            self._check_idle(managed)

    def _ping_all(self) -> Dict[str, PingResult]:
        entries = [e for e in self._config.servers if e.name]
        results: Dict[str, PingResult] = {}
        if not entries:
            return results
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
            futures = {
                pool.submit(status_ping, e.host, e.port, 3.0): e
                for e in entries
            }
            for future in concurrent.futures.as_completed(futures, timeout=15.0):
                entry = futures[future]
                try:
                    results[entry.name] = future.result()
                except Exception as exc:  # noqa: BLE001
                    results[entry.name] = PingResult(online=False, error=f'{type(exc).__name__}: {exc}')
        return results

    def _build_view(self, entry: ServerEntry, ping: Optional[PingResult]) -> ServerView:
        managed = self._managed.get(entry.name)
        view = ServerView(
            name=entry.name, host=entry.host, port=entry.port,
            role=managed.role if managed else ('watch' if entry.role in ('auto', 'watch') else entry.role),
            # 只有"负责它"的实例才写实例名；只监视的实例留空，等真正负责它的那边报上来
            instance=self.instance_name if managed is not None else '',
            local=managed is not None,
        )
        ping = ping or PingResult()
        view.auto_sleep = bool(entry.auto_sleep)
        view.always_on = bool(entry.always_on)
        if managed is not None:
            view.state = managed.state.value
            view.locked = managed.locked
            view.detail = managed.detail
            view.players = self._display_players(managed)
            view.players_max = ping.players_max if ping.players_max >= 0 else -1
            view.idle_timeout = float(managed.entry.idle_timeout or self._config.idle_timeout)
            if managed.empty_since is not None and managed.state == State.RUNNING:
                view.idle_seconds = max(0.0, time.monotonic() - managed.empty_since)
        else:
            view.idle_timeout = float(entry.idle_timeout or self._config.idle_timeout)
            if ping.online and not ping.is_fake:
                view.state = State.RUNNING.value
                view.players = ping.players_online
                view.players_max = ping.players_max
            else:
                view.state = State.SLEEPING.value if ping.is_fake else State.OFFLINE.value
                view.players = 0 if ping.is_fake else -1
                view.detail = ping.error if not ping.online else ''
        return view

    def _display_players(self, managed: ManagedServer) -> int:
        """展示用的人数：优先用状态查询结果（和玩家在服务器列表里看到的一致）。"""
        if managed.slp.online and not managed.slp.is_fake and managed.slp.players_online >= 0:
            return managed.slp.players_online
        if managed.role == 'self':
            return len(self._filter_ignored(managed.players))
        return -1

    # ================================================================== 状态维护

    def _update_managed(self, managed: ManagedServer, ping: Optional[PingResult]) -> None:
        managed.slp = ping or PingResult()
        now = time.monotonic()
        if managed.slp.online and not managed.slp.is_fake:
            # 记下"这次查询是什么时候做出来的"：它是唯一能纠正 MCDR 漏报离开的凭据
            managed.slp_at = now
            if managed.slp.players_online == 0:
                if managed.slp_zero_since is None:
                    managed.slp_zero_since = now
            else:
                managed.slp_zero_since = None
            if any(name in managed.players for name in (managed.slp.sample or [])):
                managed.players_seen_at = now
        else:
            # 查不通就不算"连续 0 人"（不能拿一个失败的结果去证明没人）
            managed.slp_zero_since = None
        if managed.action_in_progress:
            # 本实例正在执行休眠/唤醒动作：状态由动作线程负责，这里只做超时兜底
            if now - managed.state_since > self._config.startup_timeout + self._config.stop_timeout + 60:
                self._set_state(managed, State.OFFLINE, self.tr('detail.state_timeout'))
            return

        if managed.role == 'self':
            running = self._mcdr.is_server_running()
            if running:
                new_state = State.RUNNING if self._is_ready(managed, ping) else State.STARTING
                if new_state == State.RUNNING and managed.started_at is None:
                    managed.started_at = now
                    managed.empty_since = None
            else:
                if managed.external_stop:
                    # 被后台 stop 关掉的：这是"已停止"，插件不接管端口（否则又变成"休眠"）
                    new_state = State.STOPPED
                elif managed.fake and managed.fake.running:
                    new_state = State.SLEEPING
                else:
                    new_state = State.OFFLINE
            self._set_state(managed, new_state)
        else:
            if managed.slp.online and not managed.slp.is_fake:
                new_state = State.RUNNING
                if managed.started_at is None:
                    managed.started_at = now
                    managed.empty_since = None
            elif managed.fake and managed.fake.running:
                new_state = State.SLEEPING
            else:
                new_state = State.OFFLINE
            self._set_state(managed, new_state)

        if self._config.debug:
            players, note = self._effective_players(managed)
            idle = '-'
            if managed.empty_since is not None:
                idle = f'{int(now - managed.empty_since)}s'
            self._logger.debug(
                f'[侦察] {managed.name}: state={managed.state.value} players={players} '
                f'idle={idle} locked={managed.locked} ({note})'
            )

    def _is_ready(self, managed: ManagedServer, ping: Optional[PingResult]) -> bool:
        """服务端是否已经可供玩家连接。

        三个信号，任一成立即算就绪（不同服务端的日志格式差异很大，单一信号不够）：
        1. MCDR 的「启动完成」标志；
        2. 协议层状态查询能通（端口已经在收玩家）；
        3. MCDR 说进程活着、且端口能接受 TCP 连接 —— 给"服务端关掉了 enable-status、
           状态查询不通"的情况兜底。进程活着时假服务端不会占端口（有反向守卫），
           所以端口能连上就说明是真实服务端。
        """
        try:
            if self._mcdr.is_server_startup():
                return True
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'查询 MCDR 启动标志失败：{type(exc).__name__}: {exc}')
        ready = bool(ping is not None and ping.online and not ping.is_fake)
        if not ready and self._process_alive(managed):
            ready = self._port_open(managed)
            if ready and not managed.ready_fallback_logged:
                managed.ready_fallback_logged = True
                self._logger.info(f'{managed.name}: 进程在跑且端口已能连接，按"已就绪"处理')
            return ready
        if ready and not managed.ready_fallback_logged:
            managed.ready_fallback_logged = True
            self._logger.info(f'{managed.name}: MCDR 未报告启动完成，但状态查询已可用，按"已就绪"处理')
        return ready

    def _process_alive(self, managed: ManagedServer) -> bool:
        """MCDR 管的那个服务端进程是否还活着（role=command 的子服问不了）。"""
        if managed.role != 'self':
            return False
        try:
            return bool(self._mcdr.is_server_running())
        except Exception:  # noqa: BLE001
            return False

    def _port_open(self, managed: ManagedServer) -> bool:
        """便宜的"端口能不能连上"探测（不做完整的协议状态查询）。"""
        try:
            return port_open(managed.entry.host or '127.0.0.1', managed.entry.port, 1.0)
        except Exception:  # noqa: BLE001
            return False

    def _set_state(self, managed: ManagedServer, state: State, detail: str = '') -> None:
        if managed.state == state and managed.detail == detail:
            return
        old = managed.state
        managed.state = state
        managed.state_since = time.monotonic()
        if detail:
            managed.detail = detail
        elif state not in (State.OFFLINE, State.UNKNOWN, State.STOPPED):
            # OFFLINE / UNKNOWN / STOPPED 保留上一次的说明：心跳每几秒就重设一次状态，
            # 若在这里清空，玩家在浮窗里就再也看不到"为什么没跑"了
            managed.detail = ''
        if old != state:
            self._logger.info(f'{managed.name}: {old.value} -> {state.value}'
                              + (f'（{detail}）' if detail else ''))
            if state != State.RUNNING:
                managed.empty_since = None
                managed.warned = False

    def _ensure_fake_server(self, managed: ManagedServer, force: bool = False,
                            retry: bool = False) -> None:
        """确保"服务端没在跑"的时候有假服务端顶替端口，以便接住玩家的连接尝试。

        force=True 时允许用 SO_REUSEADDR 强行接管——Windows 上只要端口还残留着
        TIME_WAIT/CLOSE_WAIT 连接就拒绝 bind，而服务端刚退出时几乎总是这样，
        于是玩家能连接的时间窗会被拖到几分钟（实测线上 2 分 43 秒）。
        调用方负责先确认"端口上没有真实服务端在应答"（见 _can_force_take_over）。

        retry=True 时，绑定失败会再挂一个后台线程持续重试（见 _ensure_takeover_retry）。
        """
        need_fake = (
            self._config.fake_server.enabled
            and managed.entry.auto_wake
            # 被外部正常停掉的服不算"该接管"：管理员自己关的就是关了，
            # 端口挂个假服务端上去会把"已停止"又变成"已休眠"
            and not managed.external_stop
            # STOPPING 也算：服务端正在退出的那几秒里就该开始抢端口，
            # 一等到端口释放立刻接管（玩家能连上的时间窗越短越好）
            and managed.state in (State.STOPPING, State.SLEEPING, State.OFFLINE)
        )
        if not need_fake:
            if managed.fake is not None and managed.fake.running:
                managed.fake.stop()
            return
        if managed.fake is None:
            managed.fake = FakeServer(
                bind_host=self._config.fake_server.bind,
                port=managed.entry.port,
                status_factory=lambda protocol, name=managed.name: self._fake_status(name, protocol),
                kick_factory=lambda name=managed.name: self._fake_kick(name),
                on_login_attempt=lambda player, peer, name=managed.name: self._on_login_attempt(name, player, peer),
                logger=self._logger,
            )
        if not managed.fake.running:
            if not self._can_start_fake(managed):
                # 真实服务端还活着 —— 绝不抢先绑定：假服务端绑在具体地址（如 127.0.0.1）时，
                # 即使真实服务端占着 0.0.0.0，Windows 也会放行这次 bind，
                # 于是它会悄悄把玩家连接抢走（比"暂时连不上"严重得多）
                if retry:
                    self._ensure_takeover_retry(managed)
                return
            if managed.fake.start(force=force):
                if managed.state == State.OFFLINE:
                    self._set_state(managed, State.SLEEPING, self.tr('detail.fake_took_over'))
            else:
                if managed.state == State.SLEEPING:
                    self._set_state(managed, State.OFFLINE,
                                    self.tr('detail.fake_bind_failed', error=managed.fake.last_error))
                if retry:
                    self._ensure_takeover_retry(managed)

    def _fake_status(self, name: str, client_protocol: int = -1) -> dict:
        """假服务端的状态响应。protocol 回显客户端协议号，避免客户端显示"版本不兼容"红叉。"""
        return {
            'version': {
                'name': self.tr('fake.version_name'),
                'protocol': client_protocol if client_protocol >= 0 else 0,
            },
            'players': {'max': 0, 'online': 0, 'sample': []},
            'description': {'text': self.tr('fake.motd', server=name)},
            # 自定义标记：便于本插件与其它工具识别"这是休眠中的占位服务端"
            '_idle_guard': {
                'server': name,
                'state': 'sleeping',
                'eta': int(self._eta(self._managed.get(name))),
            },
        }

    def _fake_kick(self, name: str) -> str:
        managed = self._managed.get(name)
        return self.tr('fake.kick_message', server=name, eta=int(self._eta(managed)))

    # ================================================================== 空闲判定

    def _effective_players(self, managed: ManagedServer) -> Tuple[Optional[int], str]:
        """返回 (用于空闲判定的玩家数, 说明)；None 表示当前无法可信判断，此时绝不休眠。"""
        slp_ok = managed.slp.online and not managed.slp.is_fake
        slp_count = managed.slp.players_online if slp_ok else -1

        if managed.role == 'self':
            self._drop_stale_players(managed)
            filtered = self._filter_ignored(managed.players)
            if len(filtered) > 0:
                return len(filtered), f'MCDR 事件计数 {len(filtered)}'
            if slp_count > 0:
                # 事件说没人、查询说有人：可能是假人（被 ignore_players 忽略）或服务端隐藏了玩家列表
                sample = managed.slp.sample
                if sample and slp_count <= len(sample) and all(self._is_ignored(n) for n in sample):
                    return 0, f'查询到 {slp_count} 人但均为 ignore_players 指定的假人'
                return None, f'事件计数 0 但状态查询显示 {slp_count} 人，计数不一致'
            return 0, f'事件计数 0 / 查询 {slp_count}'

        if slp_count < 0:
            return None, '状态查询不可用'
        return slp_count, f'查询 {slp_count}'

    def _check_idle(self, managed: ManagedServer) -> None:
        """维护"连续无人多久"，并在到阈值时休眠。

        ⚠️ 空闲计时与"要不要自动休眠"是两件独立的事：
        以前这里一进门就把 `auto_sleep=False` / 已锁定的子服 `empty_since` 清成 None，
        于是那些子服的"空闲"永远显示 `-`（用户反馈"从来没见过这里有数字"就是这个）。
        现在**照常计时**（状态表/浮窗要显示这个数），只是不据此关服而已。
        """
        now = time.monotonic()
        if managed.state != State.RUNNING:
            managed.empty_since = None
            managed.warned = False
            return

        players, note = self._effective_players(managed)
        if players is None:
            # 数不清人：既不改计时、也不关服（宁可不动，也不要拿不可信的数据做判断）
            if managed.empty_since is not None:
                self._logger.debug(f'{managed.name}: {note}，暂不累计空闲时间')
            return
        if players > 0:
            if managed.empty_since is not None:
                self._logger.info(f'{managed.name}: 有玩家进入，空闲计时清零')
            managed.empty_since = None
            managed.warned = False
            return

        if managed.empty_since is None:
            managed.empty_since = now
            managed.warned = False
            self._logger.info(f'{managed.name}: 已无玩家，开始空闲计时（阈值 {int(managed.idle_timeout)} 秒）')
            return

        timeout = managed.idle_timeout or self._config.idle_timeout
        sleep_allowed = (managed.role in MANAGED_ROLES
                         and managed.entry.auto_sleep and not managed.locked
                         and not managed.entry.always_on)
        if not sleep_allowed:
            managed.warned = False
            return                      # 只计时，不管服（浮窗里仍然能看到空闲多久）

        idle = now - managed.empty_since
        if idle < timeout:
            warn = self._config.announce.warn_before_sleep
            if warn > 0 and not managed.warned and (timeout - idle) <= warn:
                managed.warned = True
                self._announce(self.tr(
                    'announce.closing_soon',
                    server=managed.name, seconds=int(timeout - idle), minutes=int(idle // 60),
                ))
            return

        # 保护期检查
        if managed.started_at is not None and now - managed.started_at < self._config.min_uptime:
            return
        if now - managed.last_wake_ts < self._config.grace_after_wake:
            return

        self._logger.info(f'{managed.name}: 连续 {int(idle)} 秒无人，执行自动休眠（{note}）')
        ok, detail = self.sleep_server(managed.name, reason='idle')
        if not ok:
            # 例如刚好有人进来了（_can_sleep 会拒绝）：把空闲计时清零重新数，
            # 否则每一轮侦察都会再试一次、把日志刷满
            managed.empty_since = None
            self._logger.info(f'{managed.name}: 本次自动休眠没有执行（{detail}），空闲计时重新开始')

    # ================================================================== 休眠

    @contextlib.contextmanager
    def _action(self, managed: ManagedServer):
        """标记「本实例正在执行休眠/唤醒动作」，期间不让轮询线程改写状态。"""
        managed.action_in_progress = True
        try:
            yield
        finally:
            managed.action_in_progress = False

    def _drop_stale_players(self, managed: ManagedServer) -> List[str]:
        """清掉"MCDR 没报告它离开、但服务端自己说没人在线"的那些名字。

        ⚠️ 线上真实踩到：新版本 Minecraft 把进出消息写成 `System chat: Pau1am left the game`，
        而 MCDR 的 vanilla_handler 对 `X left the game` 用的是 **fullmatch**，前缀对不上 →
        `on_player_left` 永远不触发。后果：玩家早就退了，插件还认为他在线，
        于是**空闲计时不开始、`!!ig sleep` 一直被拒**（用户反馈就是这个）。

        这里用"服务端自己的状态查询"兜底，但动手条件很严（宁可不动）：
        1. 那次查询成功、且不是假服务端答的；
        2. 查询说这台服 **0 人在线**，而且是**连续** `STALE_PLAYER_SECONDS` 秒都这么说
           —— 一次"0 人"可能只是玩家刚进来那一瞬间的旧结论，不能拿它把人踢掉；
        3. 这台服"变空"是发生在"最后一次确认有人"**之后**的 —— 否则那是玩家还没进来时的
           空服结论，拿它当"人已经走了"就可能把有人的服关掉。
        满足三条才清，并记一条 warning 方便排查（正常的服务端不会走到这里）。
        """
        if not managed.players:
            return []
        if not (managed.slp.online and not managed.slp.is_fake):
            return []
        if managed.slp.players_online != 0:
            return []
        zero_since = managed.slp_zero_since
        if zero_since is None or time.monotonic() - zero_since < STALE_PLAYER_SECONDS:
            return []
        if managed.players_seen_at >= zero_since:
            return []
        gone = sorted(managed.players)
        managed.players.clear()
        age = max(0, int(time.monotonic() - managed.slp_at))
        self._logger.warning(
            f'{managed.name}: 状态查询已经连续 {int(STALE_PLAYER_SECONDS)} 秒以上报"0 人在线"'
            f'（最近一次是 {age} 秒前做的），但 MCDR 没有报告 {", ".join(gone)} 离开 —— '
            f'已按查询结果清掉。多半是 MCDR 没解析出这台服务端的"离开"消息'
            f'（例如 `System chat: X left the game`）'
        )
        return gone

    def _player_names(self, managed: ManagedServer) -> List[str]:
        """能拿到名字就给出名字（用于"还有谁在线"的提示）。"""
        if managed.role == 'self':
            names = self._filter_ignored(managed.players)
            if names:
                return sorted(names)
        sample = [name for name in (managed.slp.sample or []) if not self._is_ignored(name)]
        return sorted(sample)

    def _can_sleep(self, managed: ManagedServer) -> Tuple[bool, str]:
        """能不能休眠这台子服。

        用户要求：**不能休眠还有玩家的服务器**（手动指令也拒绝）。计数不可信时同样不放行 ——
        `_effective_players` 在"事件说没人、查询说有人"这类矛盾场景下会返回 None，
        这种情况下宁可不动它。

        配成常驻（`always_on`）的子服直接拒，跟有没有人玩无关。
        """
        if managed.entry.always_on:
            return False, self.tr('action.always_on', server=managed.name)
        players, note = self._effective_players(managed)
        if players == 0:
            return True, ''
        if players is None:
            return False, self.tr('action.sleep_unknown_players', server=managed.name, detail=note)
        names = self._player_names(managed)
        if names:
            shown = ', '.join(names[:8]) + ('…' if len(names) > 8 else '')
            return False, self.tr('action.sleep_has_players', server=managed.name,
                                  count=players, players=shown)
        return False, self.tr('action.sleep_has_players_anon', server=managed.name, count=players)

    def sleep_server(self, name: str, reason: str = 'manual', by: str = 'system') -> Tuple[bool, str]:
        """休眠一个由本实例负责的子服（阻塞直到停止完成或超时）。

        ⚠️ 这个函数会阻塞（最坏几十秒），**不要在 MCDR 的命令回调里直接调用** ——
        命令层已经把它放到后台线程里执行（否则 MCDR 会报"TaskExecutor 线程十秒没有响应"）。
        """
        managed = self._managed.get(name)
        if managed is None:
            return False, self.tr('action.not_managed', server=name)
        with managed.action_lock, self._action(managed):
            if managed.state != State.RUNNING:
                return False, self.tr('action.sleep_not_needed', server=name,
                                      state=self.state_text(managed.state))
            if reason == 'idle' and (managed.locked or not managed.entry.auto_sleep):
                return False, self.tr('action.sleep_disabled', server=name)

            allowed, why_not = self._can_sleep(managed)
            if not allowed:
                self._logger.info(f'{name}: 拒绝休眠（{reason}，{by}）—— {why_not}')
                return False, why_not

            idle_minutes = 0
            if managed.empty_since is not None:
                idle_minutes = int((time.monotonic() - managed.empty_since) // 60)

            self._set_state(managed, State.STOPPING, self.tr('detail.sleep_reason', reason=reason, by=by))
            try:
                if managed.role == 'self':
                    managed.stop_event.clear()
                    self._mcdr.stop()
                else:
                    self._run_command(managed.entry.stop_command, managed.entry.working_directory,
                                      'internal.what_stop_command', name)
            except Exception as exc:  # noqa: BLE001
                self._set_state(managed, State.RUNNING, self.tr('action.stop_failed', error=exc))
                return False, self.tr('action.stop_failed', error=exc)

            if not self._wait_stopped(managed, self._config.stop_timeout):
                self._set_state(managed, State.RUNNING, self.tr('detail.wait_stop_timeout'))
                return False, self.tr('action.sleep_timeout', server=name)

            # 端口彻底释放后交给假服务端接管（等太久就转后台线程，见 _take_over_port）
            self._take_over_port(managed, timeout=min(5.0, self._config.port_release_timeout))
            if managed.fake is not None and managed.fake.running:
                self._set_state(managed, State.SLEEPING, self.tr('detail.sleeping_ready'))
            else:
                self._set_state(managed, State.OFFLINE, self.tr('detail.stopped_no_fake'))
            self._announce(self.tr(
                'announce.sleeping',
                server=name, minutes=idle_minutes, seconds=int(self._config.idle_timeout),
                detail=reason,
            ))
            self._logger.info(f'{name} 已休眠（{reason}，{by}）')
            managed.started_at = None
            return True, self.tr('action.sleep_stopped', server=name)

    def _can_start_fake(self, managed: ManagedServer) -> bool:
        """真实服务端还活着时不让假服务端绑端口。

        起因：假服务端绑的是具体地址（默认配置里是 0.0.0.0，但有人会写 127.0.0.1），
        而 Windows 允许"具体地址"再绑一次仍被 0.0.0.0 占着的端口 ——
        那时假服务端会把玩家连接从活着的服务端手里抢走，比"暂时连不上"严重得多。
        """
        if managed.role == 'self':
            try:
                return not self._mcdr.is_server_running()
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(f'{managed.name}: 查询服务端运行状态失败：{type(exc).__name__}: {exc}')
                return False
        return not port_open(managed.entry.host, managed.entry.port, 0.5)

    def _can_force_take_over(self, managed: ManagedServer) -> bool:
        """能不能用 SO_REUSEADDR 强行接管端口？

        只在"确认真实服务端已经不在跑"时才允许 —— 否则会把一个活着的服务端的端口抢走，
        那比"暂时连不上"严重得多。两道确认：

        1. 本实例负责的服务端：MCDR 报告它没在运行（进程已退出）；
        2. 端口上没有任何**真实**服务端在应答（协议层查询能区分真假，假服务端自带标记）。
        """
        if managed.role == 'self':
            try:
                if self._mcdr.is_server_running():
                    return False
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(f'{managed.name}: 查询服务端运行状态失败：{type(exc).__name__}: {exc}')
                return False
        ping = status_ping(managed.entry.host, managed.entry.port, 1.0)
        return not (ping.online and not ping.is_fake)

    def _take_over_port(self, managed: ManagedServer, timeout: Optional[float] = None) -> bool:
        """让假服务端接管端口，尽量别让玩家撞上"连接被拒绝"。

        - 先普通 bind；失败就 0.25 秒一次地重试（服务端刚退出时端口往往还占着）；
        - 3 秒还不行且确认端口上没有真实服务端在应答，就改用 SO_REUSEADDR 强行接管
          （Windows 上残留的 TIME_WAIT/CLOSE_WAIT 连接会让普通 bind 一直失败）；
        - 仍然失败就交给一个后台线程持续重试，**不再依赖侦察间隔**
          （侦察间隔默认 10 分钟，靠它重试等于玩家对着被拒的连接干等）。
        """
        budget = timeout if timeout is not None else min(30.0, self._config.port_release_timeout)
        deadline = time.monotonic() + budget
        force_after = min(3.0, budget)
        started = time.monotonic()
        # 只在"服务端正在停 / 已经停"时抢端口；一旦它开始启动（STARTING/RUNNING）就立刻收手
        while not self._stopping.is_set() and managed.state not in (State.STARTING, State.RUNNING):
            force = (time.monotonic() - started) >= force_after and self._can_force_take_over(managed)
            self._ensure_fake_server(managed, force=force)
            if managed.fake is not None and managed.fake.running:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if managed.fake is not None and managed.fake.running:
            return True
        self._logger.warning(f'{managed.name}: 端口 {managed.entry.port} 暂时移交不了给假服务端'
                             f'（{managed.fake.last_error if managed.fake else "未知原因"}），'
                             f'改为后台持续重试；这期间玩家连接会被拒绝')
        self._ensure_takeover_retry(managed)
        return False

    def _ensure_takeover_retry(self, managed: ManagedServer) -> None:
        """起一个后台线程，每 0.25 秒试一次，直到假服务端接管端口（或被唤醒）。"""
        if not self._config.fake_server.enabled or not managed.entry.auto_wake:
            return
        alive = managed.takeover_thread is not None and managed.takeover_thread.is_alive()
        if alive:
            return
        managed.takeover_thread = threading.Thread(
            target=self._takeover_retry_loop,
            args=(managed,),
            name=f'IdleGuard-Takeover-{managed.name}',
            daemon=True,
        )
        managed.takeover_thread.start()

    def _takeover_retry_loop(self, managed: ManagedServer) -> None:
        while not self._stopping.is_set():
            if managed.state in (State.STARTING, State.RUNNING):
                return          # 有人把它唤醒了，端口要留给真实服务端
            force = self._can_force_take_over(managed)
            self._ensure_fake_server(managed, force=force)
            if managed.fake is not None and managed.fake.running:
                self._logger.info(f'{managed.name}: 假服务端已接管 {managed.entry.port}，'
                                  f'玩家连接现在能触发自动唤醒')
                return
            self._stopping.wait(0.25)

    def _wait_stopped(self, managed: ManagedServer, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            if managed.role == 'self':
                if managed.stop_event.wait(0.5):
                    # 进程退出后端口可能还没释放，交给 _ensure_fake_server 重试
                    return True
                if not self._mcdr.is_server_running():
                    return True
            else:
                if not port_open(managed.entry.host, managed.entry.port, 0.5):
                    return True
                time.sleep(0.5)
        return False

    # ================================================================== 唤醒

    def wake_server(self, name: str, reason: str = 'manual', by: str = 'system',
                    trigger_player: Optional[str] = None, blocking: bool = False) -> Tuple[bool, str]:
        """唤醒一个休眠中的子服。默认异步执行（启动可能要几十秒）。

        blocking=True 时会等到服务端就绪（或失败）再返回，并给出最终结果 ——
        命令层用它来"先回一句正在启动，完成后再说结果"。
        """
        managed = self._managed.get(name)
        if managed is None:
            return False, self.tr('action.not_managed', server=name)
        if managed.locked:
            return False, self.tr('action.wake_locked', server=name, count=managed.wake_failures)
        if managed.state == State.RUNNING:
            return True, self.tr('action.already_running', server=name)
        if managed.state == State.STARTING:
            return True, self.tr('action.starting', server=name)
        if managed.state == State.STOPPING:
            return False, self.tr('action.stopping_wait', server=name)
        if reason == 'connect' and not managed.entry.auto_wake:
            return False, self.tr('action.wake_disabled', server=name)

        now = time.monotonic()
        if reason == 'connect' and now - managed.last_wake_ts < self._config.wake_cooldown:
            return True, self.tr('action.wake_cooldown', server=name)
        managed.last_wake_ts = now

        thread = threading.Thread(
            target=self._do_wake,
            args=(managed, reason, by, trigger_player),
            name=f'IdleGuard-Wake-{name}',
            daemon=True,
        )
        thread.start()
        if not blocking:
            return True, self.tr('action.wake_started', server=name)

        thread.join(timeout=self._config.startup_timeout + self._config.port_release_timeout + 10)
        return self._wake_result(managed)

    def _wake_result(self, managed: ManagedServer) -> Tuple[bool, str]:
        """把唤醒线程的最终状态翻译成给命令来源的结果。"""
        name = managed.name
        if managed.state == State.RUNNING:
            seconds = int(managed.last_startup_seconds or 0)
            if seconds > 0:
                return True, self.tr('action.wake_done', server=name, seconds=seconds)
            return True, self.tr('action.wake_done_simple', server=name)
        if managed.state in (State.STARTING, State.STOPPING):
            return True, self.tr('action.starting', server=name)
        return False, self.tr('action.wake_failed', server=name, detail=managed.detail or '?')

    def _do_wake(self, managed: ManagedServer, reason: str, by: str,
                 trigger_player: Optional[str]) -> None:
        name = managed.name
        with managed.action_lock, self._action(managed):
            if self._stopping.is_set():
                return
            # 本实例要把它重新管起来了（下面是"启服务端 / 接管端口"的动作），
            # 之前那次"被外部停止"的结论作废
            managed.external_stop = False
            self._set_state(managed, State.STARTING, self.tr('detail.wake_reason', reason=reason, by=by))
            self._announce(self.tr(
                'announce.waking',
                server=name,
                player=trigger_player or '有人',
                eta=int(self._eta(managed)),
            ))

            # 1) 先把假服务端撤下来，等端口彻底空闲，否则真实服务端会绑定失败
            if managed.fake is not None and managed.fake.running:
                managed.fake.stop()
            if not self._wait_port_free(managed, self._config.port_release_timeout):
                self._wake_failed(managed, 'detail.wake_port_busy')
                return

            # 2) 启动
            managed.launch_started = time.monotonic()
            managed.startup_event.clear()
            try:
                if managed.role == 'self':
                    self._mcdr.start()
                else:
                    self._run_command(managed.entry.start_command, managed.entry.working_directory,
                                      'internal.what_start_command', name)
            except Exception as exc:  # noqa: BLE001
                self._wake_failed(managed, 'detail.wake_start_failed', error=exc)
                return

            # 3) 等待就绪
            timeout = managed.entry.startup_timeout or self._config.startup_timeout
            if not self._wait_ready(managed, timeout):
                if self._stopping.is_set():
                    return
                self._wake_failed(managed, 'detail.wake_ready_timeout', seconds=int(timeout))
                return

            # 4) 成功
            elapsed = time.monotonic() - (managed.launch_started or time.monotonic())
            # 记下这次的真实耗时（落盘）：下次给玩家的"预计 xx 秒"就照这次写
            self._remember_startup_seconds(name, elapsed)
            managed.wake_failures = 0
            managed.started_at = time.monotonic()
            managed.empty_since = None
            managed.warned = False
            self._set_state(managed, State.RUNNING, '')
            self._logger.info(f'{name} 已就绪，本次启动耗时 {elapsed:.1f} 秒')
            self._announce(self.tr(
                'announce.opened', server=name, seconds=int(elapsed), player=trigger_player or '',
            ))

            # 单独提醒刚才被挡回去的玩家（顺便给个音效：他多半已经切去做别的事了，
            # 只看聊天栏容易漏掉这条"服开好了"）
            if self._config.announce.tell_attempt_players:
                sound = self._config.announce.sound
                now = time.monotonic()
                window = self._config.player_attempt_window
                for player in managed.attempt_players(now, window):
                    if self._has_player(player):
                        self.tell_player(player, self.tr(
                            'announce.tell_player', server=name, seconds=int(elapsed),
                        ))
                        self.play_sound(player, sound)
                    else:
                        # 他还不在任何服务端里（被挡回客户端了）：此刻 tell 发不出去，
                        # 先记下来，等他真正进服时再补（见 on_player_joined）
                        managed.pending_greetings[player] = now + window

    def _wait_port_free(self, managed: ManagedServer, timeout: float) -> bool:
        bind_host = self._config.fake_server.bind
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            if can_bind(bind_host, managed.entry.port):
                return True
            time.sleep(0.5)
        return False

    def _wait_ready(self, managed: ManagedServer, timeout: float) -> bool:
        """等待服务端真正可被玩家连接：优先 MCDR 的启动完成事件，辅以状态查询。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            if managed.startup_event.wait(0.5):
                return True
            if managed.role == 'self':
                if self._mcdr.is_server_startup():
                    return True
                if not self._mcdr.is_server_running():
                    managed.detail = self.tr('detail.process_exited')
                    return False
            result = status_ping(managed.entry.host, managed.entry.port, 1.5)
            if result.online and not result.is_fake:
                return True
            time.sleep(0.5)
        return False

    def _wake_failed(self, managed: ManagedServer, detail_key: Optional[str] = None, **args) -> None:
        """唤醒失败：记状态、放回假服务端、广播失败，并在连续失败过多时锁定。

        detail_key 为文案键；为空时用 args['detail'] 里的原始文本。
        """
        detail = self.tr(detail_key, **args) if detail_key else str(args.get('detail') or '')
        managed.wake_failures += 1
        self._logger.error(f'{managed.name} 唤醒失败（第 {managed.wake_failures} 次）：{detail}')
        self._set_state(managed, State.OFFLINE, detail)
        self._ensure_fake_server(managed, retry=True)
        self._announce(self.tr('announce.failed', server=managed.name, detail=detail))
        if managed.wake_failures >= self._config.max_wake_failures:
            managed.locked = True
            self._announce(self.tr('announce.locked', server=managed.name, count=managed.wake_failures))
            self._logger.error(f'{managed.name} 连续 {managed.wake_failures} 次启动失败，已锁定自动唤醒')

    # ---- 实测启动耗时（用来给玩家报 ETA）----

    def _resolve_data_dir(self) -> Optional[str]:
        """MCDR 给插件的 data 目录（`config/idle_guard`）；拿不到就不落盘，只留内存。"""
        getter = getattr(self._mcdr, 'get_data_folder', None)
        if not callable(getter):
            return None
        try:
            return os.path.abspath(getter())
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'拿不到插件数据目录，启动耗时不落盘：{type(exc).__name__}: {exc}')
            return None

    def _load_startup_seconds(self) -> Dict[str, List[float]]:
        if self._data_dir is None:
            return {}
        path = os.path.join(self._data_dir, _STARTUP_STATS_FILE)
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(f'启动耗时记录读取失败（忽略）：{type(exc).__name__}: {exc}')
            return {}
        result: Dict[str, List[float]] = {}
        if isinstance(data, dict):
            for name, value in data.items():
                if not isinstance(name, str):
                    continue
                # 新格式：{"survival": [11.7, 12.4, 18.1]}；
                # 旧格式：{"survival": 11.7}（单个数字）——当成只有一个样本，
                # 老用户的文件不用手动改，跑几次之后样本自然攒起来
                raw = value if isinstance(value, list) else [value]
                samples = [float(item) for item in raw
                           if isinstance(item, (int, float)) and not isinstance(item, bool)
                           and item > 0]
                if samples:
                    result[name] = samples[-_STARTUP_SAMPLES_MAX:]
        return result

    def _remember_startup_seconds(self, name: str, seconds: float) -> None:
        """记下这次实测的启动耗时（内存 + 落盘），ETA 用这些样本的中位数。"""
        seconds = max(0.5, round(float(seconds), 1))
        samples = self._startup_seconds.setdefault(name, [])
        samples.append(seconds)
        del samples[:-_STARTUP_SAMPLES_MAX]
        managed = self._managed.get(name)
        if managed is not None:
            managed.last_startup_seconds = seconds
        self._logger.info(
            f'{name}: 启动耗时 {seconds:.1f} 秒（最近 {len(samples)} 次 '
            f'{" / ".join(f"{value:.1f}" for value in samples)}），'
            f'下次报 ETA 用中位数 {_median(samples):.1f} 秒'
        )
        if self._data_dir is None:
            return
        path = os.path.join(self._data_dir, _STARTUP_STATS_FILE)
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(self._startup_seconds, handle, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'启动耗时记录写入失败：{type(exc).__name__}: {exc}')

    def _eta(self, managed: Optional[ManagedServer]) -> float:
        """预计启动耗时：取最近若干次实测的**中位数**。

        为什么不是"上一次"：服务端的启动耗时抖动很大（世界加载、mod 初始化、磁盘缓存冷热），
        照搬上一次经常差出好几倍，玩家看到的就是"说 12 秒，结果快 1 分钟"。
        中位数对个别离谱样本不敏感，报出来更接近"通常要等多久"。
        一个样本都没有时才退到配置里的兜底值。
        """
        if managed is not None:
            samples = self._startup_seconds.get(managed.name)
            if samples:
                return _median(samples)
        return self._config.fallback_startup_seconds

    def eta_of(self, server: str) -> float:
        """给玩家看的"预计启动耗时"：优先用实测值，否则用配置里的兜底值。"""
        return self._eta(self._managed.get(server))

    # ================================================================== 假服务端回调

    def _on_login_attempt(self, name: str, player: Optional[str], peer: str) -> None:
        managed = self._managed.get(name)
        if managed is None:
            return
        now = time.monotonic()
        # 名字是客户端握手时自己报的，先过滤：它会被拼进 tellraw / playsound 命令里执行
        player = safe_player_name(player) or None
        managed.record_attempt(player, now, self._config.player_attempt_window)
        self._logger.info(f'{player or "未知玩家"}（{peer}）正在连接休眠中的 {name}，'
                          f'自动唤醒={managed.entry.auto_wake} 锁定={managed.locked}')
        if managed.entry.auto_wake and not managed.locked:
            self.wake_server(name, reason='connect', by='player', trigger_player=player)

    # ================================================================== 通知

    def _peer_addresses(self) -> List[str]:
        own = self._bus_bind
        return [e.bus for e in self._config.servers if e.bus and e.bus != own]

    def _announce(self, text: str) -> None:
        if not self._config.announce.enabled or not text:
            return
        self._say_local(text)
        self._bus.broadcast_async(self._peer_addresses(), {'type': 'announce', 'kind': 'say', 'text': text})

    def tell_player(self, player: str, text: str) -> None:
        if not text:
            return
        self._tell_local(player, text)
        self._bus.broadcast_async(self._peer_addresses(), {
            'type': 'announce', 'kind': 'tell', 'player': player, 'text': text,
        })

    def play_sound(self, player: str, sound: str) -> None:
        """给某个玩家放一个音效（这玩家在哪个子服就由那个实例来放）。

        用来提醒"你刚才想进的服已经开好了" —— 玩家很可能已经切到别的界面，
        光有聊天栏文字容易被漏掉。
        """
        if not sound or not safe_player_name(player):
            return
        self._play_local(player, sound)
        self._bus.broadcast_async(self._peer_addresses(), {
            'type': 'announce', 'kind': 'sound', 'player': player, 'sound': sound,
        })

    def _play_local(self, player: str, sound: str) -> None:
        if not self._has_player(player):
            return
        # 在他自己的位置放，否则控制台执行时声源在世界原点，距离一远就听不见了
        command = f'execute at {player} run playsound {sound} master {player} ~ ~ ~ 1 1'
        try:
            self._mcdr.execute(command)
            self._logger.debug(f'已向 {player} 播放音效 {sound}')
        except Exception as exc:  # noqa: BLE001 - 放音效失败不能影响别的事
            self._logger.debug(f'给 {player} 放音效失败：{type(exc).__name__}: {exc}')

    def _say_local(self, text: str) -> None:
        self._logger.info(f'[通知] {text}'.replace('§', '&'))
        try:
            if self._mcdr.is_server_running():
                self._mcdr.say(text)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'本服广播失败：{type(exc).__name__}: {exc}')

    def _tell_local(self, player: str, text: str) -> None:
        if not safe_player_name(player) or not self._has_player(player):
            return
        try:
            self._mcdr.tell(player, text)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'提醒玩家 {player} 失败：{type(exc).__name__}: {exc}')

    def _has_player(self, player: str) -> bool:
        """判断玩家是否在本实例能看到的某个子服里（决定要不要用 tellraw 通知他）。"""
        if not player:
            return False
        lowered = player.lower()
        for managed in self._managed.values():
            if managed.role == 'self':
                if any(name.lower() == lowered for name in managed.players):
                    return True
        for ping in self._last_ping.values():
            if ping.online and not ping.is_fake:
                if any(name.lower() == lowered for name in ping.sample):
                    return True
        return False

    # ================================================================== 总线

    def _on_bus_message(self, message: dict) -> Optional[dict]:
        mtype = message.get('type')
        if mtype == 'announce':
            text = str(message.get('text') or '')
            if message.get('kind') == 'tell':
                player = str(message.get('player') or '')
                self._tell_local(player, text)
                return {'ok': True, 'delivered': self._has_player(player)}
            if message.get('kind') == 'sound':
                player = str(message.get('player') or '')
                self._play_local(player, str(message.get('sound') or ''))
                return {'ok': True, 'delivered': self._has_player(player)}
            self._say_local(text)
            return {'ok': True}
        if mtype == 'state':
            return {
                'ok': True,
                'instance': self.instance_name,
                'servers': [v.to_dict() for v in self.local_views()],
            }
        if mtype == 'action':
            return self._handle_remote_action(message)
        return {'ok': False, 'reason': f'未知消息类型 {mtype!r}'}

    def _handle_remote_action(self, message: dict) -> dict:
        """处理别的实例发来的操作请求。

        ⚠️ 必须**立刻应答**：休眠要等服务端停稳 + 端口移交（几十秒），
        而请求方等应答只有总线超时那么久（默认 3 秒）。以前在这里同步执行，
        于是请求方等到超时、什么也没收到（结果就是那句误报"本实例不负责 X"）。
        真正的执行放到后台线程，请求方靠轮询状态确认结果（见 _wait_remote_state）。
        """
        action = message.get('action')
        name = message.get('server')
        by = str(message.get('by') or self.tr('command.source.remote'))
        managed = self._managed.get(name)
        not_managed = (False, self.tr('action.not_managed', server=name))

        if action in ('wake', 'sleep'):
            if managed is None:
                ok, detail = not_managed
            elif managed.action_in_progress:
                ok, detail = True, self.tr('action.accepted')
            elif action == 'sleep':
                # ⚠️ 先**同步**过一遍"现在到底能不能休眠"再转后台：
                # 以前这里不检查、直接回"已受理"，`_can_sleep` 的拒绝只留在本实例日志里，
                # 请求方于是对着状态干等到 `_wait_remote_state` 超时（线上反馈：
                # 玩家在别的服敲了 !!ig sleep，只看到"正在休眠…"，三分钟后才等到"等待超时"，
                # 真正的原因"这台服上还有人"从没回到他那边）。
                allowed, why_not = self.precheck_sleep(name)
                if allowed:
                    ok, detail = self._sleep_in_background(name, reason='remote', by=by)
                else:
                    ok, detail = False, why_not
                    self._logger.info(f'{name}: 拒绝休眠（remote，{by}）—— {why_not}')
            else:
                ok, detail = self.wake_server(name, reason='remote', by=by)
        elif action in ('lock', 'unlock'):
            if managed is None:
                ok, detail = not_managed
            else:
                managed.locked = (action == 'lock')
                if not managed.locked:
                    managed.wake_failures = 0
                ok, detail = True, self.tr('action.locked' if managed.locked else 'action.unlocked',
                                           server=name)
        else:
            ok, detail = False, self.tr('action.unknown_op', action=repr(action))
        return {'ok': ok, 'reason': detail, 'instance': self.instance_name}

    def precheck_sleep(self, name: str) -> Tuple[bool, str]:
        """**不阻塞**地判断"现在到底能不能休眠"，返回 (可以, 不能的理由)。

        与 ``sleep_server`` 开头那几项检查保持一致（重复检查无害：真正执行时还会在
        action_lock 里再查一遍）。远程请求必须靠它**立刻**拿到真实结果，
        否则请求方只能等 `_wait_remote_state` 超时，玩家看不到真正的原因。
        """
        managed = self._managed.get(name)
        if managed is None:
            return False, self.tr('action.not_managed', server=name)
        if managed.state != State.RUNNING:
            return False, self.tr('action.sleep_not_needed', server=name,
                                  state=self.state_text(managed.state))
        return self._can_sleep(managed)

    def _sleep_in_background(self, name: str, reason: str, by: str) -> Tuple[bool, str]:
        """把休眠丢到后台线程执行，立刻回一句"已受理"。"""
        threading.Thread(
            target=self.sleep_server,
            args=(name, reason, by),
            name=f'IdleGuard-Sleep-{name}',
            daemon=True,
        ).start()
        return True, self.tr('action.accepted')

    # ================================================================== 对外查询

    def local_views(self) -> List[ServerView]:
        """本实例视角下的所有子服状态。

        本实例负责的子服**实时**生成快照（刚执行完 `!!ig sleep` 就能看到"已休眠"，
        不必等下一轮侦察）；其它子服用最近一次侦察的快照。
        """
        views: List[ServerView] = []
        for entry in self._config.servers:
            managed = self._managed.get(entry.name)
            if managed is not None:
                views.append(self._build_view(entry, self._last_ping.get(entry.name)))
                continue
            view = self._views.get(entry.name)
            views.append(view if view is not None else self._build_view(entry, None))
        return views

    def status_views(self) -> Dict[str, ServerView]:
        """返回所有子服的最新状态（见 live_views）。"""
        return self.live_views()[0]

    def live_views(self) -> Tuple[Dict[str, ServerView], Set[str]]:
        """返回 (全部子服快照, 其中"数据可信"的子服名集合)。

        可信 = 这份数据是刚算出来的，而不是"上一次侦察留下的旧快照"：

        - 本实例负责的子服：实时生成（本实例就是权威）；
        - 别的实例负责的子服：**整体采用**对方应答里的那一份（状态/人数/空闲都是对方刚算的）。
          以前这里只借对方的"实例名"、数据继续用本地旧快照，于是出现线上那个问题：
          刚把某台子服休眠掉，`!!ig status` 里它还是"运行中" + 开关仍然是 [休眠]
          —— 因为本地那份快照是上一轮侦察（默认 10 分钟前）拍的。
        - 对方没应答（实例不在线）时才退回本地旧快照，且**不算**可信数据，
          这样命令层就不会拿陈旧状态去拒绝一个其实该执行的操作。
        """
        merged: Dict[str, ServerView] = {v.name: v for v in self.local_views()}
        live: Set[str] = {name for name, view in merged.items() if view.role in MANAGED_ROLES}
        for reply in self._bus.broadcast(self._peer_addresses(), {'type': 'state'}, timeout=2.0):
            instance = str(reply.get('instance') or '')
            for data in reply.get('servers') or []:
                if not isinstance(data, dict):
                    continue
                view = ServerView.from_dict(data, instance=instance)
                target = merged.get(view.name)
                if view.role in MANAGED_ROLES:
                    # 对方说"我在管它" —— 它才是权威，整份采用
                    view.local = False
                    merged[view.name] = view
                    live.add(view.name)
                elif target is None:
                    merged[view.name] = view
                elif not target.instance and view.instance:
                    target.instance = view.instance
        return merged, live

    # ================================================================== 事件入口

    def on_server_startup(self) -> None:
        """MCDR 报告本服启动完成。"""
        for managed in self._managed.values():
            if managed.role != 'self':
                continue
            managed.startup_event.set()
            # 服务端起来了 —— 之前那次"被外部停止"的结论作废，本实例重新接管它
            managed.external_stop = False
            # MCDR 都说了"启动完成"，状态就没必要再等下一轮心跳/侦察（线上反馈：
            # 服务端明明开好了，状态表还显示"启动中"好几分钟）
            if not managed.action_in_progress and managed.state != State.RUNNING:
                self._logger.info(f'{managed.name}: MCDR 报告启动完成，状态直接置为运行中')
                managed.started_at = time.monotonic()
                managed.empty_since = None
                self._set_state(managed, State.RUNNING, '')

    def on_server_stop(self, return_code: int) -> None:
        if self._stopping.is_set():
            # MCDR 正在退出，此时服务端停止属于正常流程，不需要通知
            return
        for managed in self._managed.values():
            if managed.role == 'self':
                managed.stop_event.set()
                managed.players.clear()
                managed.started_at = None
                if managed.state == State.STOPPING:
                    # sleep_server 会接着做后续处理
                    continue
                if return_code == 0:
                    # 正常退出（后台敲 stop、或服务端自己优雅退出）——这是"已停止"，不是"休眠"。
                    # 插件不接管端口，也不阻止 MCDR 按默认行为随服务端一起退出：
                    # 以前这里一律当"意外停止"处理，于是 stop 之后端口被假服务端占住、
                    # 状态显示"已休眠"、MCDR 还被强行留住，管理员发现 stop 关不掉了。
                    self._logger.info(f'{managed.name}: 服务端被正常停止（返回码 0），'
                                      f'按"已停止"处理 —— 插件不接管端口，'
                                      f'玩家连接不会被自动开服（要休眠请用 !!ig sleep）')
                    managed.external_stop = True
                    if managed.fake is not None and managed.fake.running:
                        managed.fake.stop()
                    self._set_state(managed, State.STOPPED,
                                    self.tr('detail.server_stopped_manual', code=return_code))
                    if self._config.announce.enabled:
                        self._announce(self.tr('announce.stopped_manual', server=managed.name))
                    continue
                # 异常退出（崩溃 / 被外部强杀）：立刻让假服务端接管，等待玩家唤醒
                self._logger.warning(f'{managed.name} 意外停止（返回码 {return_code}），'
                                     f'将保持休眠状态，有人连接时自动启动')
                managed.external_stop = False
                self._keep_mcdr_alive(managed)
                self._set_state(managed, State.OFFLINE,
                                self.tr('detail.server_stopped', code=return_code))
                self._ensure_fake_server(managed, retry=True)
                if self._config.announce.enabled:
                    self._announce(self.tr('announce.stopped', server=managed.name))

    def _keep_mcdr_alive(self, managed: ManagedServer) -> None:
        """服务端意外停止时，别让 MCDR 跟着退出。

        MCDR 在**每次服务端启动**时都会打上 `EXIT_AFTER_STOP` 标记（官方文档原话：
        "The flag value will be set to True everytime when the server starts"），
        于是服务端一停 MCDR 就退出 —— 崩溃/被外部关掉时，假服务端也跟着没了，
        "玩家连接自动开服"这条链就断了（状态表在别的实例上也只能显示过期数据）。

        插件自己停服走的是 `mcdr.stop()`（MCDR 会清掉那个标记，所以那种情况 MCDR 是活的）。
        这里只在"插件确实要接管端口并自动唤醒"时清一次标记，其余情况不动 MCDR 的默认行为。
        """
        if not (self._config.fake_server.enabled and managed.entry.auto_wake):
            return
        setter = getattr(self._mcdr, 'set_exit_after_stop_flag', None)
        if setter is None:
            return
        try:
            setter(False)
            self._logger.info(f'{managed.name}: 已阻止 MCDR 随服务端退出（假服务端继续接管端口，'
                              f'玩家连接时可自动启动）')
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'清除 EXIT_AFTER_STOP 标记失败：{type(exc).__name__}: {exc}')

    def on_player_joined(self, player: str) -> None:
        now = time.monotonic()
        for managed in self._managed.values():
            if managed.role == 'self':
                managed.players.add(player)
                managed.players_seen_at = now
                self._greet_after_wake(managed, player, now)

    def _greet_after_wake(self, managed: ManagedServer, player: str, now: float) -> None:
        """他进服了 —— 如果这台服是他刚才触发唤醒的那台，就补一句"已经开好了"。

        为什么需要补：唤醒完成的提示是在服务端内部广播 + 单独 tell 的，而那时他刚被
        假服务端挡回客户端，既不在这个服务端里、也不在任何服务端里，两条消息他都收不到
        （线上反馈："启动好后并没有给我任何提示"）。
        """
        expiry = managed.pending_greetings.pop(player, None)
        if expiry is None:
            return
        for stale in [name for name, ts in managed.pending_greetings.items() if now > ts]:
            managed.pending_greetings.pop(stale, None)
        if now > expiry:
            return      # 太久之后才进来，就不补了（免得莫名其妙弹一句）
        self.tell_player(player, self.tr(
            'announce.joined_after_wake', server=managed.name,
            seconds=int(managed.last_startup_seconds or 0),
        ))
        self.play_sound(player, self._config.announce.sound)

    def on_player_left(self, player: str) -> None:
        for managed in self._managed.values():
            if managed.role == 'self':
                managed.players.discard(player)

    #: 服务端自己打印的进出消息。
    #: ⚠️ 为什么还要自己解析：MCDR 的 vanilla_handler 用的是 `(?P<name>[^ ]+) left the game`
    #: 配 **fullmatch**，而新版 Minecraft 把这类消息写成 `System chat: Pau1am left the game`，
    #: 前缀一多就完全对不上 → `on_player_left` 永不触发（线上实测：玩家退出后插件一直以为
    #: 有人在，空闲计时不开始、`!!ig sleep` 被无限拒绝）。进服那边 MCDR 走的是
    #: `X[ip] logged in with entity id N at (...)` 那条，所以只有"离开"会漏。
    _ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
    _PLAYER_NAME = r'[A-Za-z0-9_]{1,16}'
    _LEFT_PATTERNS = (
        re.compile(r'^(?:System chat: )?(?P<name>' + _PLAYER_NAME + r') left the game$'),
        re.compile(r'^(?:System chat: )?(?P<name>' + _PLAYER_NAME + r') lost connection: .*$'),
    )
    _JOINED_PATTERNS = (
        re.compile(r'^(?:System chat: )?(?P<name>' + _PLAYER_NAME + r') joined the game$'),
    )

    def on_server_output(self, info) -> None:
        """从服务端输出里自己识别玩家进/退，补 MCDR 可能漏掉的那条。

        只在"服务端自己打印的进出消息"上动作：玩家的聊天/命令行（`info.is_user` /
        `info.is_player`）一律跳过，免得有人把这句话打进聊天里就能操纵计数。
        与 MCDR 自己的事件重复时是幂等的（set 的 add/discard）。
        """
        try:
            if info is None or getattr(info, 'is_user', False) or getattr(info, 'is_player', False):
                return
            if not any(m.role == 'self' for m in self._managed.values()):
                return
            content = self._ANSI_RE.sub('', str(getattr(info, 'content', '') or '')).strip()
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'解析服务端输出失败：{type(exc).__name__}: {exc}')
            return
        if not content:
            return
        for pattern in self._JOINED_PATTERNS:
            matched = pattern.match(content)
            if matched:
                self.on_player_joined(matched.group('name'))
                return
        for pattern in self._LEFT_PATTERNS:
            matched = pattern.match(content)
            if matched:
                self.on_player_left(matched.group('name'))
                return

    # ================================================================== 工具

    def _filter_ignored(self, players) -> List[str]:
        result = []
        for name in players:
            if not any(pattern.search(name) for pattern in self._ignore_patterns):
                result.append(name)
        return result

    def _is_ignored(self, name: str) -> bool:
        return any(pattern.search(name) for pattern in self._ignore_patterns)

    def _run_command(self, command: Optional[str], working_directory: Optional[str],
                     what_key: str, server: str) -> None:
        what = self.tr(what_key, server=server)
        if not command:
            raise ValueError(self.tr('internal.missing_command', what=what))
        cwd = working_directory or None
        self._logger.info(f'执行{what}：{command}（工作目录 {cwd or os.getcwd()}）')
        subprocess.Popen(command, cwd=cwd, shell=True)  # noqa: S602 - 命令来自管理员自己的配置

    # ---- 供命令模块使用的操作入口（会自行广播到其它实例） ----

    def request_action(self, action: str, server: str, by: str = 'console',
                       wait: bool = False) -> Tuple[bool, str]:
        """执行一个操作（sleep / wake / lock / unlock）。

        ``wait=True`` 时会等到操作真正有结果（服务端停稳 / 启动就绪，或远程实例报出新状态）
        再返回 —— 命令层把它放在后台线程里调用，所以不会卡住 MCDR。
        """
        if server in self._managed:
            if action == 'wake':
                return self.wake_server(server, reason='manual', by=by, blocking=wait)
            if action == 'sleep':
                return self.sleep_server(server, reason='manual', by=by)
            if action in ('lock', 'unlock'):
                managed = self._managed[server]
                if action == 'lock':
                    managed.locked = True
                else:
                    managed.locked = False
                    managed.wake_failures = 0
                return True, self.tr('action.locked' if managed.locked else 'action.unlocked', server=server)
            return False, self.tr('action.unknown_op', action=repr(action))

        # 广播给**别的**实例，只有负责该服的实例会应答 ok。
        # ⚠️ 必须排除自己：否则本实例会先回一句"本实例不负责 X"，
        # 而远端真正执行休眠/唤醒要几十秒、早就超过总线超时了 —— 于是那句误报会盖掉真正的结果
        # （线上真实踩到：`!!ig sleep Creative` 明明生效了，却回"本实例不负责 Creative"）。
        replies = [reply for reply in
                   (self._bus.send(address, {'type': 'action', 'action': action, 'server': server, 'by': by})
                    for address in self._peer_addresses())
                   if reply]
        accepted = next((reply for reply in replies if reply.get('ok')), None)
        if accepted is not None:
            detail = str(accepted.get('reason') or self.tr('action.accepted'))
            if not wait or action in ('lock', 'unlock'):
                return True, detail
            return self._wait_remote_state(server, action, detail)
        if replies:
            # 有实例应答了，但都说自己不负责（比如它那条的 role 是 watch）
            return False, str(replies[0].get('reason') or self.tr('action.failed'))

        # 一个应答都没有：负责它的实例不在线，或者这条没配总线地址
        addresses = self._peer_addresses()
        entry = next((e for e in self._config.servers if e.name == server), None)
        role = entry.role if entry is not None else 'watch'
        # 提示要说对病因：role 已经是 auto 时，再劝人"把 role 改成 auto"是废话
        # （线上真踩到过：管理员照做了，结果提示一字未变）
        hint = self.tr('action.hint_auto' if role == 'auto' else 'action.hint_own',
                       server=server, role=role)
        # 连失败原因一起记：只说"没人应答"没法排查，得看清是连不上、鉴权失败还是对端没回
        last_error = getattr(self._bus, 'last_error', None)
        tried = []
        for address in addresses:
            reason = last_error(address) if callable(last_error) else ''
            tried.append(f'{address}（{reason}）' if reason else address)
        self._logger.warning(self.tr('action.no_owner_none_answered', server=server, action=action,
                                      addresses='、'.join(tried) if tried else '无'))
        if entry is not None and not entry.bus:
            return False, self.tr('action.no_remote_bus', server=server, hint=hint)
        return False, self.tr('action.no_owner', server=server, hint=hint)

    def _wait_remote_state(self, server: str, action: str, accepted_detail: str) -> Tuple[bool, str]:
        """等远程实例上的目标状态出现（或超时）。"""
        timeout = (self._config.startup_timeout if action == 'wake' else self._config.stop_timeout) + 30.0
        wanted = {State.RUNNING.value} if action == 'wake' else {State.SLEEPING.value, State.OFFLINE.value}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            view = self._lookup_view(server)
            if view is not None and view.state in wanted:
                if action == 'wake':
                    return True, self.tr('action.wake_done_simple', server=server)
                return True, self.tr('action.sleep_stopped', server=server)
            time.sleep(2.0)
        return False, self.tr('action.wait_timeout', server=server, seconds=int(timeout))

    def _lookup_view(self, server: str) -> Optional[ServerView]:
        try:
            return self.status_views().get(server)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'查询 {server} 状态失败：{type(exc).__name__}: {exc}')
            return None

    def state_of(self, server: str) -> Optional[ManagedServer]:
        return self._managed.get(server)

    # ================================================================== 二次确认

    def confirm_ttl(self) -> float:
        return max(5.0, float(self._config.confirm_timeout))

    def set_pending_action(self, who: str, action: str, server: str) -> PendingAction:
        """登记一个"等你确认"的动作（`!!ig confirm` 才真正执行）。"""
        pending = PendingAction(action=action, server=server, by=who,
                                deadline=time.monotonic() + self.confirm_ttl())
        with self._pending_lock:
            self._pending[who] = pending
        return pending

    def take_pending_action(self, who: str) -> Tuple[Optional[PendingAction], bool]:
        """取出并清除待确认动作；第二个返回值表示"它是不是已经过期了"。"""
        with self._pending_lock:
            pending = self._pending.pop(who, None)
        if pending is None:
            return None, False
        return pending, time.monotonic() > pending.deadline

    def clear_pending_action(self, who: str) -> Optional[PendingAction]:
        with self._pending_lock:
            return self._pending.pop(who, None)

    def reload_plugin(self, plugin_id: Optional[str] = None) -> None:
        """让 MCDR 重新加载本插件（配置热重载）。"""
        self._mcdr.reload_plugin(plugin_id or self.plugin_id)

    @property
    def self_name(self) -> Optional[str]:
        return self._self_name

    @property
    def config(self) -> Config:
        return self._config
