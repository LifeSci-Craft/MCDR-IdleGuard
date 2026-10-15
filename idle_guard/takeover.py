"""假服务端与端口接管：休眠期间顶替游戏端口、接住玩家连接、把端口抢回来的重试。

从 `supervisor.py` 拆出来的（那个文件原本 2000+ 行）。这些方法都以 `Supervisor` 的成员
身份运行（`self` 就是 Supervisor），只是换个文件放，方便按主题阅读与修改：

- `_ensure_fake_server`：需要顶端口时把假服务端挂上（真实服务端还活着就绝不抢）；
- `_on_login_attempt`：玩家连进来 → 记下名字并触发唤醒；
- `_can_start_fake` / `_can_force_take_over`：两个反向守卫，绝不去抢活着的服务端的端口；
- `_take_over_port` / `_ensure_takeover_retry`：端口残留时短重试 + 后台持续重试。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .fakeserver import FakeServer
from .config import FAKE_SERVER_BIND
from .models import ManagedServer, State, safe_player_name


class TakeoverMixin:
    def _ensure_fake_server(self, managed: ManagedServer, force: bool = False,
                            retry: bool = False) -> None:
        """确保"服务端没在跑"的时候有假服务端顶替端口，以便接住玩家的连接尝试。

        force=True 时允许用 SO_REUSEADDR 强行接管——Windows 上只要端口还残留着
        TIME_WAIT/CLOSE_WAIT 连接就拒绝 bind，而服务端刚退出时几乎总是这样，
        于是玩家能连接的时间窗会被拖到几分钟（实测线上 2 分 43 秒）。
        调用方负责先确认"端口上没有真实服务端在应答"（见 _can_force_take_over）。

        retry=True 时，绑定失败会再挂一个后台线程持续重试（见 _ensure_takeover_retry）。
        """
        if self._stopping.is_set():
            # 插件正在卸载：绝不能再把端口抢回来。否则表现为"插件已经卸下了，
            # 端口上却还挂着假服务端"（轮询线程可能在 shutdown 的 join 超时后仍在跑）。
            return
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
                bind_host=FAKE_SERVER_BIND,
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
            # ⚠️ bind 之前必须**再确认一次**状态：上面的 need_fake 判断是在进入本函数时做的，
            # 而 _can_start_fake 在 role != self 时会做一次最长 0.5 秒的 TCP 探测 ——
            # 这半秒里唤醒流程完全可能把状态推到 STARTING 并开始启动真实服务端
            # （_take_over_port 与后台重试线程都不持有 managed.action_lock）。
            # 少了这一道，就可能把"正在启动的真实服务端"的端口抢过来：轻则唤醒失败，
            # Windows 上还会出现两个 socket 并存、玩家被假服务端分走。
            if self._stopping.is_set() or managed.state in (State.STARTING, State.RUNNING):
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
        return not self._probe_port(managed.entry.host, managed.entry.port, 0.5)

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
        ping = self._probe_status(managed.entry.host, managed.entry.port, 1.0)
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
        """起一个后台线程，按退避序列试到假服务端接管端口（有总时长上限，见 _takeover_retry_loop）。"""
        if self._stopping.is_set():
            return
        if not self._config.fake_server.enabled or not managed.entry.auto_wake:
            return
        alive = managed.takeover_thread is not None and managed.takeover_thread.is_alive()
        if alive:
            return
        # 让状态表 / 诊断页能看出"这一条正在后台抢端口"，而不是只显示一个"未运行"
        # （这条文案以前是零引用的死键）
        if managed.state == State.OFFLINE:
            self._set_state(managed, State.OFFLINE, self.tr('detail.takeover_retrying'))
        managed.takeover_thread = threading.Thread(
            target=self._takeover_retry_loop,
            args=(managed,),
            name=f'IdleGuard-Takeover-{managed.name}',
            daemon=True,
        )
        managed.takeover_thread.start()

