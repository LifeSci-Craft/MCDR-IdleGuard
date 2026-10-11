"""服务端"正在忙"时不做自动休眠（备份 / 保存保护）。

起因：插件原来只看"有没有玩家"，完全不知道服务端正在干什么 ——
备份跑到一半被休眠打断，那份备份就废了（装的备份插件越多越容易撞上）。

做法：监听服务端输出，命中 `busy_patterns` 就记下"先别自动关它"，`busy_hold_seconds`
之后自动失效（不需要各家插件再打一条"结束"日志，那种文案靠不住）。
**只拦自动休眠**：管理员手敲的 `!!ig sleep` 照常执行。
"""
from __future__ import annotations

import time

import pytest

from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


class FakeInfo:
    """模仿 MCDR 传给 on_info 的 Info 对象。"""

    def __init__(self, content: str, *, is_user: bool = False, is_player: bool = False) -> None:
        self.content = content
        self.is_user = is_user
        self.is_player = is_player


def build(*, idle_timeout: float = 0.2, patterns=None, hold: float = 300.0):
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=9999.0,
        idle_timeout=idle_timeout,
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        busy_hold_seconds=hold,
        bus=BusConfig(bind=None, token='t', timeout=0.5),
        fake_server=FakeServerConfig(enabled=True, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True, auto_wake=True),
        ],
    )
    if patterns is not None:
        config.busy_patterns = list(patterns)
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


@pytest.fixture
def pinger(monkeypatch):
    def fake_status(host, port, timeout=1.5, **kwargs):
        return PingResult(online=True, players_online=0, players_max=20, sample=[],
                          version_name='Fabric')

    import idle_guard.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, 'status_ping', fake_status)


def running(supervisor):
    supervisor.start()
    managed = supervisor.state_of('Survival')
    assert wait_until(lambda: managed.state == State.RUNNING, 10)
    return managed


def test_a_backup_line_marks_the_server_busy(pinger):
    supervisor, stub = build()
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world...'))

        assert managed.busy_until > time.monotonic(), '没有记下"它在忙"'
        assert 'Backing up' in managed.busy_reason
        assert '正在忙' in stub.logger.text(), stub.logger.text
    finally:
        supervisor.shutdown()


def test_an_ordinary_line_does_not_mark_it_busy(pinger):
    supervisor, _stub = build()
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('Steve joined the game'))
        assert managed.busy_until == 0.0, '普通输出不该触发忙碌判定'
    finally:
        supervisor.shutdown()


def test_a_players_chat_line_cannot_fake_it(pinger):
    """玩家自己在聊天里打"Backing up"不能影响判定。"""
    supervisor, _stub = build()
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('<Steve> Backing up world', is_player=True))
        assert managed.busy_until == 0.0
    finally:
        supervisor.shutdown()


def test_busy_server_is_not_auto_slept(pinger):
    supervisor, stub = build(idle_timeout=0.2)
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world'))

        supervisor._check_idle(managed)          # 开始计时（阈值 0.2 秒）
        time.sleep(0.3)                          # 越过阈值
        supervisor._check_idle(managed)          # 又一轮：到点了，但它在忙

        assert stub.stop_calls == 0, '服务端在忙，不该被自动休眠'
        assert managed.state == State.RUNNING
    finally:
        supervisor.shutdown()


def test_it_sleeps_again_once_the_server_is_free(pinger):
    """忙完（hold 到期）之后要能照常自动休眠，不能一忙就永久不关。"""
    supervisor, stub = build(idle_timeout=0.2)
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world'))
        supervisor._check_idle(managed)
        time.sleep(0.3)
        supervisor._check_idle(managed)
        assert stub.stop_calls == 0, '前置条件不成立：这一轮本该被"忙"拦下'

        managed.busy_until = time.monotonic() - 1.0        # 假装 hold 已经到期
        supervisor._check_idle(managed)

        assert stub.stop_calls >= 1, '忙完之后没有恢复自动休眠'
    finally:
        supervisor.shutdown()


def test_manual_sleep_is_not_blocked_by_busy(pinger):
    """管理员明确要关的时候不拦（只拦自动判定）。"""
    supervisor, stub = build()
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world'))

        ok, _detail = supervisor.request_action('sleep', 'Survival', by='测试')

        assert ok, '手动休眠不该被"忙"挡下'
        assert wait_until(lambda: stub.stop_calls >= 1, 10)
    finally:
        supervisor.shutdown()


def test_busy_shows_up_in_the_sleep_precheck(pinger):
    supervisor, _stub = build()
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world'))

        checks = supervisor.sleep_checklist('Survival')

        assert any(not ok and '忙' in text for ok, text in checks), checks
    finally:
        supervisor.shutdown()


def test_patterns_can_be_switched_off(pinger):
    supervisor, _stub = build(patterns=[])
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world'))
        assert managed.busy_until == 0.0
    finally:
        supervisor.shutdown()


def test_a_custom_pattern_works(pinger):
    supervisor, _stub = build(patterns=[r'(?i)我的备份开始了'])
    try:
        managed = running(supervisor)
        supervisor.on_server_output(FakeInfo('我的备份开始了，请稍候'))
        assert managed.busy_until > time.monotonic()
    finally:
        supervisor.shutdown()
