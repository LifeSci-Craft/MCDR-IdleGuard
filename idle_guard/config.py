"""
配置定义

MCDR 会在 `config/idle_guard/config.json` 生成默认配置文件；改动后可用 `!!ig reload` 热重载。
同一份配置可以（也推荐）复制到每个子服的 MCDR 实例上使用：
每个实例通过 `role` / 自动端口嗅探决定"自己是哪一个"，其余条目按对等关系参与通知。

配置里**只有行为开关，没有文案**：所有面对玩家/管理员的文字（MOTD、休眠提示、
命令输出等）都在插件的 `lang/<语言>.json` 里（见 `i18n.py`），
用 `language` 字段选语言；想改文案就在 `config/idle_guard/lang/` 下放同名覆盖文件。
"""
from dataclasses import dataclass, field
from typing import List, Optional

from mcdreforged.api.utils import Serializable


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
    #: 覆盖全局的空闲阈值（秒）
    idle_timeout: Optional[float] = None
    #: 覆盖全局的启动超时（秒）
    startup_timeout: Optional[float] = None


@dataclass
class BusConfig(Serializable):
    """实例间通信总线配置。"""

    #: 本实例监听的地址，如 "127.0.0.1:25601"；留空 = 不接收跨服消息（仍可向别人发）
    #: 填 "auto" 时会自动取「本实例负责的子服条目」上写的 bus 地址，方便各实例共用同一份配置
    bind: Optional[str] = None
    #: 通信口令，所有实例必须一致
    token: str = 'idle_guard'
    #: 单次请求超时（秒）
    timeout: float = 3.0


@dataclass
class FakeServerConfig(Serializable):
    """假服务端配置（顶替休眠中的真实服务端端口）。

    这里只留行为开关；MOTD / 版本名 / 玩家看到的提示文字都在 lang/<语言>.json。
    """

    enabled: bool = True
    #: 绑定地址。真实服务端一般监听 0.0.0.0，这里保持默认即可
    bind: str = '0.0.0.0'


@dataclass
class AnnounceConfig(Serializable):
    """通知行为开关（文案在 lang/<语言>.json）。"""

    enabled: bool = True
    #: 休眠前多少秒先预告一次（0 = 不预告，直接发"已休眠"）
    warn_before_sleep: float = 0.0
    #: 是否单独提醒"试图进入但被挡住"的玩家
    tell_attempt_players: bool = True
    #: 服务端开好之后，给那位被挡住的玩家放的音效（空字符串 = 不播放）
    #: 用原版音效 id 即可，例如 minecraft:block.note_block.pling（音符盒）、
    #: minecraft:block.note_block.bell（钟）、minecraft:entity.experience_orb.pickup（经验球）
    sound: str = 'minecraft:block.note_block.pling'


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
    #: 启动后保护期：服务端启动完成后的这段时间内不判定空闲（秒）
    min_uptime: float = 300.0
    #: 被唤醒后的保护期：这段时间内不再休眠（秒），避免刚开就关
    grace_after_wake: float = 300.0
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
    #: 记住"谁尝试进入过"的时长（秒），用于服务端起来后单独提醒
    player_attempt_window: float = 600.0
    #: 统计玩家数时忽略的玩家名（正则，例如假人：["^bot_", "^Bot"]）
    ignore_players: List[str] = field(default_factory=list)
    #: 连续启动失败多少次后锁定自动唤醒
    max_wake_failures: int = 3
    #: `!!ig sleep` 发出后，等多久之内可以用 `!!ig confirm` 确认（秒）
    confirm_timeout: float = 30.0
    #: Velocity 配置文件（`velocity.toml`）的路径。
    #: `auto` = 自动在 MCDR 工作目录附近查找；`off` / `''` = 关闭这个同步。
    #: 打开时，插件启动会把 velocity.toml 的 `[servers]` 段里**还没写进 servers[]** 的子服
    #: 自动补进来（名字、地址、端口），已有条目的设置一律不动。
    velocity_config: str = 'auto'
    #: 打印详细调试日志
    debug: bool = False

    bus: BusConfig = field(default_factory=BusConfig)
    fake_server: FakeServerConfig = field(default_factory=FakeServerConfig)
    announce: AnnounceConfig = field(default_factory=AnnounceConfig)

    #: 子服务器列表。下面是一份可直接改用的示例：
    servers: List[ServerEntry] = field(default_factory=lambda: [
        ServerEntry(
            name='lobby', host='127.0.0.1', port=25565, role='watch',
            auto_sleep=False, bus='127.0.0.1:25600',
        ),
        ServerEntry(
            name='survival', host='127.0.0.1', port=25566, role='auto',
            auto_sleep=True, auto_wake=True, bus='127.0.0.1:25601',
        ),
    ])


#: role 的合法取值
VALID_ROLES = ('auto', 'self', 'command', 'watch')
