"""后台手动停服 ≠ 休眠（回归测试）。

线上反馈：在 MCDR 控制台敲 `stop` 关掉服务端后，插件把它当成"进入休眠" ——
状态表显示"已休眠"、假服务端把端口占了、控制台还在提示"将保持休眠状态"，
更麻烦的是插件顺手清掉了 MCDR 的 EXIT_AFTER_STOP 标记，于是 `stop` 再也关不掉整个实例
（管理员原话："不允许我使用 stop"）。

修法：按服务端的退出码区分两种停止 ——
- 返回码 0（后台 `stop` / 服务端自己优雅退出）=> `stopped`：插件**不接管端口**、
  **不碰** EXIT_AFTER_STOP 标记、通知也换成"不是休眠"的说法；
- 返回码非 0（崩溃 / 被强杀）=> 保留原来的"意外停止"兜底
  （假服务端接管 + 留住 MCDR，好让玩家连接时能自动开服）。
"""
from __future__ import annotations

import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import ServerView, State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


def build(*, state_tick: float = 0.3):
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=state_tick,
        idle_timeout=9999.0,
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=True, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=True),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True, auto_wake=True),
        ],
    )
    stub = StubMCDR()
    supervisor = Supervisor(stub, config, plugin_version='test')
    return supervisor, stub


@pytest.fixture
def pinger(monkeypatch):
    """状态查询换成可控实现：服务端在线、0 人（不干扰"停止"这件事本身）。"""
    state = {'online': True, 'players': 0, 'is_fake': False}

    def fake_status(host, port, timeout=1.5, **kwargs):
        return PingResult(online=state['online'], players_online=state['players'],
                          players_max=20, sample=[], version_name='Fabric',
                          is_fake=state['is_fake'])

    import idle_guard.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, 'status_ping', fake_status)
    return state


def running(supervisor):
    """把实例带到 RUNNING，返回那一行。"""
    supervisor.start()
    managed = supervisor.state_of('Survival')
    assert wait_until(lambda: managed.state == State.RUNNING, 10), \
        f'前置条件不成立：状态是 {managed.state}'
    return managed


# --------------------------------------------------------------------------- #
# ① 手动停止 = 已停止，不是休眠
# --------------------------------------------------------------------------- #

def test_manual_stop_is_not_a_sleep(pinger):
    supervisor, stub = build()
    try:
        managed = running(supervisor)
        stub.running = False              # 服务端进程退出（后台敲 stop）
        stub.exit_after_stop = True       # MCDR 每次服务端启动都会打上这个标记

        supervisor.on_server_stop(0)      # MCDR 报告停止，返回码 0

        assert managed.state == State.STOPPED, f'手动停止被当成了 {managed.state.value}'
        assert managed.external_stop is True
        assert stub.exit_after_stop is True, \
            '不该清 EXIT_AFTER_STOP：清了之后 stop 就关不掉整个实例了'
        assert not (managed.fake and managed.fake.running), \
            '手动停止不该让假服务端接管端口（那又变成"休眠"了）'
    finally:
        supervisor.shutdown()


def test_manual_stop_announces_it_is_not_a_sleep(pinger):
    supervisor, stub = build()
    try:
        running(supervisor)
        stub.running = False
        supervisor.on_server_stop(0)
        log = stub.logger.text()
        assert '不是休眠' in log, f'通知没有说清这不是休眠：{log[-400:]}'
    finally:
        supervisor.shutdown()


def test_the_heartbeat_does_not_turn_it_back_into_sleeping(pinger):
    """心跳每几秒就重算一次状态 —— 它不能把"已停止"又推导成 offline/sleeping。"""
    supervisor, stub = build(state_tick=0.2)
    try:
        managed = running(supervisor)
        stub.running = False
        supervisor.on_server_stop(0)
        assert managed.state == State.STOPPED

        time.sleep(1.0)                   # 跑过好几个心跳周期

        assert managed.state == State.STOPPED, f'心跳把它改成了 {managed.state.value}'
        assert not (managed.fake and managed.fake.running), '心跳又把假服务端挂上去了'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 崩溃 / 被强杀仍然走老的兜底（别把这条能力一起删了）
# --------------------------------------------------------------------------- #

def test_crash_still_gets_the_old_failsafe(pinger):
    supervisor, stub = build()
    try:
        managed = running(supervisor)
        stub.running = False
        stub.exit_after_stop = True

        supervisor.on_server_stop(15)     # 非 0 = 崩溃 / 被 kill

        assert managed.external_stop is False
        assert managed.state in (State.OFFLINE, State.SLEEPING)
        assert stub.exit_after_stop is False, '崩溃时必须留住 MCDR，否则没人能把服务端拉起来'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ③ 可逆：服务端重新起来 / 被唤醒后就恢复托管
# --------------------------------------------------------------------------- #

def test_starting_the_server_again_clears_the_stopped_mark(pinger):
    supervisor, stub = build()
    try:
        managed = running(supervisor)
        stub.running = False
        supervisor.on_server_stop(0)
        assert managed.state == State.STOPPED

        stub.running = True
        supervisor.on_server_startup()    # MCDR：服务端起来了

        assert managed.external_stop is False
        assert managed.state == State.RUNNING
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ④ 状态表：已停止要能一键开回来，且"再休眠"没有意义
# --------------------------------------------------------------------------- #

def test_status_table_offers_wake_for_a_stopped_server(pinger):
    assert State.STOPPED.value in cmd_mod._STATE_COLOURS, '状态表缺这个状态的颜色'
    action, _key, _colour = cmd_mod._SWITCH_ACTIONS[State.STOPPED.value]
    assert action == 'wake', '已停止的服应当给"开启"按钮'


def test_precheck_on_a_stopped_server(pinger):
    supervisor, _stub = build()
    try:
        view = ServerView(name='Survival', host='127.0.0.1', port=1, role='self',
                          state=State.STOPPED.value, players=0, instance='Survival', local=True)
        tr = supervisor.tr
        assert cmd_mod._precheck_message('sleep', State.STOPPED.value, view, tr, 'Survival') is not None, \
            '对一个已经停止的服"再休眠"应当直接回话'
        assert cmd_mod._precheck_message('wake', State.STOPPED.value, view, tr, 'Survival') is None, \
            '已停止的服必须允许 !!ig wake 把它开起来'
    finally:
        supervisor.shutdown()
