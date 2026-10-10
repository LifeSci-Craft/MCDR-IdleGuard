"""状态刷新与空闲计时的回归测试（v1.0.0）。

线上反馈两条：
1. 关掉服务端后状态表还写"运行中"、开好之后一直写"启动中" ——
   根因：状态只在**侦察周期**里更新（默认 10 分钟），中间发生的变化要等下一轮才看见。
   修法：① 轮询线程加一个秒级的"状态心跳"（只查本实例负责的子服）；
   ② MCDR 报告启动完成时立刻置为运行中；③ 就绪判定再加一条"端口能连上"的兜底。
2. "空闲"那一列永远只有 `-` —— 根因：`_check_idle` 一进门就把
   `auto_sleep=False`（或已锁定）的子服 `empty_since` 清成 None，于是这些子服永远不计时。
   修法：**照常计时**，只是不据此关服；同时那一列从表里拿掉，信息搬进"状态"的浮窗。
"""
from __future__ import annotations

import time

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


class Source:
    def __init__(self, console: bool = True) -> None:
        self.is_console = console
        self.replies: list = []
        self.widgets: list = []

    def reply(self, text) -> None:
        self.widgets.append(text)
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True


def build(*, auto_sleep: bool = True, state_tick: float = 0.4):
    """单实例环境：本实例管 Survival（MCDR 直管），Creative 由别人管。"""
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,          # 故意把侦察周期拉长：下面验的就是"不等侦察也能更新"
        state_tick=state_tick,
        idle_timeout=9999.0,
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=auto_sleep),
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch'),
        ],
    )
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


@pytest.fixture
def pinger(monkeypatch):
    """把状态查询换成可控的假实现（默认：在线、0 人）。"""
    state = {'online': True, 'players': 0, 'is_fake': False}

    def fake_ping(host, port, timeout=3.0):
        if not state['online']:
            return PingResult(online=False, error='模拟：连不上')
        return PingResult(online=state['online'], players_online=state['players'],
                          players_max=20, sample=[], version_name='Paper', motd='mock')

    monkeypatch.setattr(supervisor_module, 'status_ping', fake_ping)
    monkeypatch.setattr(supervisor_module, 'port_open', lambda host, port, timeout=1.0: state['online'])
    return state


# --------------------------------------------------------------------------- #
# 1. 状态心跳：不用等侦察周期
# --------------------------------------------------------------------------- #

def test_state_follows_the_server_without_waiting_for_a_poll(pinger):
    """服务端进程退出后，状态应当**秒级**变成"已休眠/未运行"，而不是等下一个侦察周期。"""
    supervisor, stub = build()
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        stub.running = False                      # 服务端被关掉了（MCDR 立刻知道）
        started = time.monotonic()
        assert wait_until(lambda: supervisor.state_of('Survival').state != State.RUNNING, 5), \
            '服务端都停了，状态还停在运行中'
        assert time.monotonic() - started < 5
        assert supervisor.state_of('Survival').state == State.OFFLINE
    finally:
        supervisor.shutdown()


def test_state_becomes_running_as_soon_as_the_port_accepts(pinger):
    """开通后只要端口能连上（或 MCDR 报启动完成），状态就该变运行中。"""
    supervisor, stub = build()
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        managed = supervisor.state_of('Survival')
        # 模拟"重启中"：进程还在但还没就绪
        stub.started_up = False
        pinger['online'] = False
        assert wait_until(lambda: managed.state == State.STARTING, 5), managed.state
        stub.started_up = True                    # MCDR 报告启动完成
        assert wait_until(lambda: managed.state == State.RUNNING, 3), managed.state
    finally:
        supervisor.shutdown()


def test_on_server_startup_updates_the_state_immediately(pinger):
    """on_server_startup 是事件驱动的，不该再等心跳/侦察。"""
    supervisor, stub = build(state_tick=0.0)      # 心跳关掉，纯粹验事件
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        managed = supervisor.state_of('Survival')
        managed.state = State.STARTING            # 假装刚重启，卡在启动中
        stub.started_up = True
        supervisor.on_server_startup()
        assert managed.state == State.RUNNING, 'MCDR 都报启动完成了，状态还是启动中'
    finally:
        supervisor.shutdown()


def test_state_tick_defaults_to_five_seconds():
    """心跳默认 5 秒；用户配置里不写这一项也能生效。"""
    assert Config().state_tick == 5.0
    assert Config().check_interval == 600.0


# --------------------------------------------------------------------------- #
# 2. 空闲计时：与"要不要自动休眠"解耦
# --------------------------------------------------------------------------- #

def test_idle_timer_runs_even_when_auto_sleep_is_off(pinger):
    """用户报的 bug：关掉自动休眠的子服，空闲永远是 `-`。现在照样计时。"""
    supervisor, stub = build(auto_sleep=False)
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)
        assert wait_until(lambda: managed.empty_since is not None, 5), 'auto_sleep=False 就不计时了'
        view = supervisor.status_views()['Survival']
        assert view.idle_seconds >= 0, '浮窗里要能看到空闲多久'
        assert view.auto_sleep is False
    finally:
        supervisor.shutdown()


def test_idle_timer_resets_when_someone_joins(pinger):
    supervisor, stub = build()
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.empty_since is not None, 5)
        supervisor.on_player_joined('Steve')
        assert wait_until(lambda: managed.empty_since is None, 5), '有人进来后空闲计时没清零'
        supervisor.on_player_left('Steve')
        assert wait_until(lambda: managed.empty_since is not None, 5), '玩家走了应当重新计时'
    finally:
        supervisor.shutdown()


def test_untrusted_player_count_keeps_the_timer_but_never_sleeps(pinger):
    """人数不可信时：计时不动（不做判断），也绝不关服。"""
    supervisor, _stub = build()
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.empty_since is not None, 5)
        before = managed.empty_since
        # 事件说没人、查询说有人 -> 计数不一致 -> None
        pinger['players'] = 3
        managed.slp = PingResult(online=True, players_online=3, players_max=20, sample=['Alex'])
        supervisor._check_idle(managed)
        assert managed.empty_since == before, '数不清人时不该把计时清掉'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 3. 状态表：没有空闲列，信息在"状态"的浮窗里
# --------------------------------------------------------------------------- #

def test_status_table_has_no_idle_column_but_the_hover_does(pinger):
    supervisor, _stub = build(auto_sleep=False)
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.empty_since is not None, 5)
        managed.empty_since = time.monotonic() - 12 * 60          # 空闲 12 分钟
        managed.state = State.RUNNING

        source = Source(console=True)
        cmd_mod._cmd_status(source, supervisor)
        header = source.replies[1]
        assert '空闲' not in header, header
        assert '状态' in header and '开关' in header, header

        view = supervisor.status_views()['Survival']
        cells = cmd_mod.status_cells(view, 1, supervisor.tr, '!!ig', chat=True)
        hover = cells[3].hover or ''
        assert '空闲 12 分钟' in hover and '阈值' in hover, hover
        assert '自动休眠' in hover, hover          # auto_sleep=False 要说明"不会自动关"
        assert len(cells) == 7, '列数：编号/子服/角色/状态/人数/备注/开关'
    finally:
        supervisor.shutdown()


def test_remote_managed_row_is_labelled_as_another_instance(pinger):
    """远端实例报上来的 role=self 是"**它**自己是 self"，显示成本实例会误导（线上截图就是两行都写本实例）。"""
    supervisor, _stub = build()
    try:
        tr = supervisor.tr
        view = supervisor._build_view(supervisor.config.servers[1], None)
        view.role, view.local, view.instance, view.state = 'self', False, 'Creative', State.RUNNING.value
        cell = cmd_mod._role_cell(view, tr)
        assert cell.text == tr('role.remote_self'), cell

        view.local = True                          # 真·本实例
        assert cmd_mod._role_cell(view, tr).text == tr('role.self')

        view.role, view.local = 'watch', False
        assert cmd_mod._role_cell(view, tr).text == tr('role.watch')
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 4. 服务端意外停止时，MCDR 不该跟着退出
# --------------------------------------------------------------------------- #

def test_mcdr_is_kept_alive_when_the_server_dies(pinger):
    """MCDR 每次服务端启动都会打上 EXIT_AFTER_STOP；崩溃/外部关服时它就会跟着退。

    这时假服务端也没了，"玩家连接自动开服"整条链断掉 —— 插件要清掉那个标记。
    """
    supervisor, stub = build()
    try:
        calls = []
        stub.set_exit_after_stop_flag = calls.append      # MCDR 的公开 API
        supervisor.config.fake_server.enabled = True      # 插件确实要接管端口
        supervisor.config.fake_server.bind = '127.0.0.1'
        supervisor.state_of('Survival').entry.auto_wake = True
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        supervisor.on_server_stop(15)                     # 模拟崩溃退出
        assert calls == [False], '没有阻止 MCDR 随服务端退出'
        assert '已阻止 MCDR 随服务端退出' in stub.logger.text()
    finally:
        supervisor.shutdown()


def test_plugin_initiated_stop_does_not_touch_the_flag(pinger):
    """插件自己休眠时（state=STOPPING）不碰这个标记 —— MCDR 的 stop() 本来就处理好了。"""
    supervisor, stub = build()
    try:
        calls = []
        stub.set_exit_after_stop_flag = calls.append
        supervisor.config.fake_server.enabled = True
        supervisor.state_of('Survival').entry.auto_wake = True
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)
        managed.state = State.STOPPING
        supervisor.on_server_stop(0)
        assert calls == []
    finally:
        supervisor.shutdown()


def test_watched_servers_are_refreshed_by_the_heartbeat(pinger):
    """只监视的子服也要跟着刷新：对方实例挂了时，这一行不能一直显示旧状态。"""
    supervisor, _stub = build()
    try:
        supervisor.start()
        watched = 'Creative'
        assert wait_until(lambda: supervisor._views.get(watched) is not None, 5)
        supervisor._views[watched].state = State.RUNNING.value      # 假装是几分钟前的旧快照
        pinger['online'] = False                                    # 那台服其实已经没了
        assert wait_until(
            lambda: supervisor._views[watched].state == State.OFFLINE.value, 5), \
            '心跳没有刷新"只监视"那一行'
    finally:
        supervisor.shutdown()
