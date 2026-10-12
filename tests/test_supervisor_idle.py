"""调度核心（Supervisor）的休眠/唤醒状态机测试。

这里用一个 StubMCDR 顶替真实的 MCDR 接口，用假的 status_ping 顶替对其它子服的探测，
但假服务端、端口绑定、登录打断都是真实的 —— 也就是说这些测试覆盖了绝大多数真实路径。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult, status_ping as real_status_ping
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, login_attempt, reason_text


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def make_env(monkeypatch):
    """构造一个被测环境：返回 (stub, supervisor, port, state)。"""
    created = []

    def _make(*, idle_timeout: float = 0.6, check_interval: float = 1.0,
              ignore_players=(), extra_servers=(), **config_overrides) -> SimpleNamespace:
        port = free_port()
        state = {'slp_online': 0, 'sample': [], 'online': True}

        def fake_ping(host, port_arg, timeout=3.0):
            if not state['online']:
                return PingResult(online=False, error='模拟：端口无人响应')
            return PingResult(
                online=True,
                players_online=state['slp_online'],
                players_max=20,
                sample=list(state['sample']),
                version_name='Paper',
                motd='模拟服务端',
            )

        monkeypatch.setattr(supervisor_module, 'status_ping', fake_ping)

        stub = StubMCDR()
        entries = [
            ServerEntry(name='survival', host='127.0.0.1', port=port, role='self',
                        auto_sleep=True, auto_wake=True),
            *extra_servers,
        ]
        config = Config(
            instance_name='survival',
            check_interval=check_interval,
            idle_timeout=idle_timeout,
            min_uptime=0.0,
            startup_timeout=10.0,
            stop_timeout=5.0,
            port_release_timeout=5.0,
            wake_cooldown=0.0,
            fallback_startup_seconds=5.0,
            player_attempt_window=60.0,
            ignore_players=list(ignore_players),
            debug=True,
            bus=BusConfig(bind=None),
            fake_server=FakeServerConfig(enabled=True),
            announce=AnnounceConfig(enabled=True),
            servers=entries,
            **config_overrides,
        )
        supervisor = Supervisor(stub, config)
        created.append(supervisor)
        return SimpleNamespace(stub=stub, sup=supervisor, port=port, state=state)

    yield _make
    for supervisor in created:
        try:
            supervisor.shutdown()
        except Exception:  # noqa: BLE001
            pass


def test_idle_server_gets_sleep_and_fake_server_takes_over(make_env):
    env = make_env()
    env.sup.start()
    assert wait_until(lambda: env.stub.stop_calls == 1), '空闲后没有自动关服'
    assert wait_until(lambda: env.sup.state_of('survival').state == State.SLEEPING)

    # 真实服务端停止后，假服务端必须能顶替端口并回答状态查询
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake)
    result = real_status_ping('127.0.0.1', env.port, 1.0)
    assert result.online and result.is_fake and result.players_online == 0
    assert result.marker['server'] == 'survival'

    # 休眠通知：本服服务端已经停了，所以本服不会 say，通知要靠总线广播给其它子服；
    # 这里用日志断言「通知已生成」
    assert '[通知]' in env.stub.logger.text()
    assert '休眠' in env.stub.logger.text()


def test_login_attempt_triggers_wake_and_notifies(make_env, monkeypatch):
    env = make_env()
    env.sup.start()
    assert wait_until(lambda: env.stub.stop_calls == 1)
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake)

    # 玩家此时在另一个子服（用状态查询的 sample 模拟"玩家在线"），尝试进入休眠中的 survival
    env.state['sample'] = ['Steve']
    env.sup.poll_once()          # 立刻侦察一轮，让 sample 生效（真实环境里由轮询线程完成）
    reason = login_attempt('127.0.0.1', env.port, name='Steve')
    text = reason_text(reason)
    assert 'survival' in text, text

    # 应当自动触发启动
    assert wait_until(lambda: env.stub.start_calls == 1), '登录尝试没有触发自动启动'
    env.stub.started_up = True
    env.sup.on_server_startup()

    assert wait_until(lambda: env.sup.state_of('survival').state == State.RUNNING)
    assert wait_until(lambda: '已开启' in env.stub.said_text())
    # 启动完成后应当单独提醒刚才被挡回去的玩家
    assert wait_until(lambda: any(player == 'Steve' for player, _ in env.stub.told))
    assert env.sup.state_of('survival').last_startup_seconds is not None


def test_second_idle_cycle_after_wake(make_env):
    """唤醒后再无人，应当能再次休眠（覆盖端口二次接管的路径）。"""
    env = make_env()
    env.sup.start()
    assert wait_until(lambda: env.stub.stop_calls == 1)
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake)

    login_attempt('127.0.0.1', env.port, name='Alex')
    assert wait_until(lambda: env.stub.start_calls == 1)
    env.stub.started_up = True
    env.sup.on_server_startup()
    assert wait_until(lambda: env.sup.state_of('survival').state == State.RUNNING)

    # Alex 从未真正进入，之后继续无人 -> 应当第二次休眠
    assert wait_until(lambda: env.stub.stop_calls == 2, timeout=15.0), '第二次空闲没有休眠'
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake, timeout=10.0)


def test_no_sleep_when_counts_disagree(make_env):
    """MCDR 事件说没人、但状态查询说有玩家时，宁可不休眠。"""
    env = make_env()
    env.state['slp_online'] = 2
    env.state['sample'] = ['Alice', 'Bob']
    env.sup.start()
    time.sleep(4.0)
    assert env.stub.stop_calls == 0
    assert env.sup.state_of('survival').state == State.RUNNING
    assert '计数不一致' in env.stub.logger.text() or '不一致' in env.stub.logger.text()


def test_ignored_bots_do_not_block_sleep(make_env):
    """ignore_players 命中的假人不应阻止休眠。"""
    env = make_env(ignore_players=[r'^bot_'])
    env.sup.on_player_joined('bot_1')
    env.sup.on_player_joined('bot_2')
    assert wait_until(lambda: env.sup.state_of('survival').players == {'bot_1', 'bot_2'})
    env.sup.start()
    assert wait_until(lambda: env.stub.stop_calls == 1), '假人被计入玩家数，导致没有休眠'


def test_real_player_prevents_sleep(make_env):
    env = make_env()
    env.sup.on_player_joined('RealPlayer')
    env.sup.start()
    time.sleep(4.0)
    assert env.stub.stop_calls == 0
    assert env.sup.state_of('survival').state == State.RUNNING


def test_lock_prevents_sleep_and_unlock_restores(make_env):
    env = make_env()
    env.sup.request_action('lock', 'survival')
    env.sup.start()
    time.sleep(4.0)
    assert env.stub.stop_calls == 0
    env.sup.request_action('unlock', 'survival')
    assert wait_until(lambda: env.stub.stop_calls == 1, timeout=10.0)


def test_manual_sleep_and_wake(make_env):
    env = make_env(idle_timeout=9999.0)
    env.sup.start()
    assert wait_until(lambda: env.sup.state_of('survival').state == State.RUNNING)
    ok, detail = env.sup.request_action('sleep', 'survival', by='测试')
    assert ok, detail
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake)

    ok, detail = env.sup.request_action('wake', 'survival', by='测试')
    assert ok, detail
    assert wait_until(lambda: env.stub.start_calls == 1)
    env.stub.started_up = True
    env.sup.on_server_startup()
    assert wait_until(lambda: env.sup.state_of('survival').state == State.RUNNING)


def test_unexpected_stop_starts_fake_server(make_env):
    """服务端崩溃/被外部停掉时，假服务端要接管端口，等待玩家唤醒。"""
    env = make_env(idle_timeout=9999.0)
    env.sup.start()
    assert wait_until(lambda: env.sup.state_of('survival').state == State.RUNNING)
    env.stub.running = False           # 模拟进程消失
    env.sup.on_server_stop(1)          # MCDR 的停止事件
    assert wait_until(lambda: real_status_ping('127.0.0.1', env.port, 1.0).is_fake)
    assert wait_until(lambda: env.sup.state_of('survival').state == State.SLEEPING)


def test_status_views_show_idle_countdown(make_env):
    env = make_env(idle_timeout=9999.0)
    env.sup.start()
    assert wait_until(lambda: env.sup.status_views()['survival'].idle_seconds >= 0)
    view = env.sup.status_views()['survival']
    assert view.players == 0
    assert view.idle_timeout == pytest.approx(9999.0)
