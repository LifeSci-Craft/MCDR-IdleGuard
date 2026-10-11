"""唤醒后的提示到底去哪了（线上反馈：玩家连接休眠服触发唤醒，启动好后什么都没收到）。

他连接休眠的子服时，是被**假服务端**挡回客户端的 —— 那一刻他不在任何服务端里。而唤醒完成的提示：
- `announce.opened` 是在服务端内部广播的（他不在这个服务端里，看不到）；
- `tell` 给他的那条又因为"玩家不在线"被丢掉了。

于是他从头到尾什么都没收到。修法：把他记成"待招呼"，等他真正进服时补一条 + 音效。
"""
from __future__ import annotations

import time

import pytest

from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


def build():
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=0.3,
        idle_timeout=9999.0,
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        player_attempt_window=600.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=True, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=True, tell_attempt_players=True),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True, auto_wake=True),
        ],
    )
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


@pytest.fixture
def pinger(monkeypatch):
    def fake_status(host, port, timeout=1.5, **kwargs):
        return PingResult(online=True, players_online=0, players_max=20, sample=[],
                          version_name='Fabric')

    import idle_guard.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, 'status_ping', fake_status)


def slept_then_woken(supervisor, stub, trigger_player: str = 'Pau1am'):
    """把实例带到：休眠 → 有人尝试连接 → 唤醒完成。返回那一行。"""
    supervisor.start()
    managed = supervisor.state_of('Survival')
    assert wait_until(lambda: managed.state == State.RUNNING, 10)
    assert supervisor.sleep_server('Survival', reason='manual', by='测试')[0]
    assert wait_until(lambda: managed.state == State.SLEEPING, 10), managed.state

    managed.attempts[trigger_player] = time.monotonic()   # 他刚刚尝试连接过（被假服务端挡回）
    stub.started_up = False
    assert supervisor.wake_server('Survival', reason='manual', by='测试')[0]
    assert wait_until(lambda: stub.start_calls >= 1, 10), '没有真的去启动服务端'
    stub.started_up = True
    supervisor.on_server_startup()
    assert wait_until(lambda: managed.state == State.RUNNING, 15), managed.state
    return managed


def test_wake_notice_is_deferred_when_the_player_is_not_online_yet(pinger):
    supervisor, stub = build()
    try:
        managed = slept_then_woken(supervisor, stub)
        assert 'Pau1am' in managed.pending_greetings, '没有记住"他还没进服，等进来再补提示"'
        assert 'Pau1am' not in [player for player, _text in stub.told], \
            '他当时不在线，这一条 tell 本来也发不出去'
    finally:
        supervisor.shutdown()


def test_the_deferred_notice_arrives_when_he_joins(pinger):
    supervisor, stub = build()
    try:
        managed = slept_then_woken(supervisor, stub)

        supervisor.on_player_joined('Pau1am')

        told = [text for player, text in stub.told if player == 'Pau1am']
        assert told, f'进服时没有补上提示：{stub.told}'
        assert any('已开启' in text for text in told), told
        assert any('playsound' in command for command in stub.executed), \
            f'补提示应当顺便给个音效：{stub.executed}'
        assert 'Pau1am' not in managed.pending_greetings, '待办没有清掉'
    finally:
        supervisor.shutdown()


def test_a_stale_deferred_notice_is_dropped(pinger):
    """过了很久才进来就不要再补了，免得莫名其妙弹一句。"""
    supervisor, stub = build()
    try:
        managed = slept_then_woken(supervisor, stub)
        managed.pending_greetings['Pau1am'] = time.monotonic() - 1.0      # 已经过期

        supervisor.on_player_joined('Pau1am')

        assert [text for player, text in stub.told if player == 'Pau1am'] == []
        assert 'Pau1am' not in managed.pending_greetings
    finally:
        supervisor.shutdown()
