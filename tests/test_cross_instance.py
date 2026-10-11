"""跨实例操作（总线）与假服务端接管端口的回归测试。

覆盖两个线上真实踩到的问题：

1. `!!ig sleep Creative` 在「管理 Survival 的实例」上执行：休眠确实生效了，
   但回给用户的是"本实例不负责 Creative"。原因有两个，缺一不可：
   - 广播时把自己也当成目标 → 自己先回一句"本实例不负责 X"；
   - 远端同步执行休眠（几十秒）→ 早超过总线超时，真正的应答收不到。
2. 休眠后假服务端迟迟绑不上端口（Windows 上残留连接会让 bind 一直失败），
   于是玩家 `/server <子服>` 只会被拒、也**触发不了唤醒**（线上实测空窗 2 分 43 秒）。
"""
from __future__ import annotations

import time

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.fakeserver import FakeServer
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


def _config(name: str, bus_port: int, peer_port: int) -> Config:
    """A 实例负责 Survival、只监视 Creative；B 实例反过来（与线上拓扑一致）。"""
    own, peer = (('Survival', peer_port), ('Creative', bus_port)) if name == 'Survival' \
        else (('Creative', peer_port), ('Survival', bus_port))
    entries = [
        ServerEntry(name=own[0], host='127.0.0.1', port=own[1], role='self', auto_sleep=True),
        ServerEntry(name=peer[0], host='127.0.0.1', port=peer[1], role='watch', auto_sleep=False),
    ]
    return Config(
        instance_name=name,
        language='zh_cn',
        check_interval=0.3,
        idle_timeout=9999.0,
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        wake_cooldown=0.0,
        fallback_startup_seconds=5.0,
        debug=True,
        bus=BusConfig(bind=f'127.0.0.1:{bus_port}', token='t', timeout=2.0),
        fake_server=FakeServerConfig(enabled=True, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=entries,
    )


@pytest.fixture
def two_instances(monkeypatch):
    """两个真实（总线走真实 socket）的实例：A 管 Survival，B 管 Creative。"""
    bus_a, bus_b = free_port(), free_port()
    port_survival, port_creative = free_port(), free_port()
    state = {'survival_online': 1, 'creative_online': 1}

    def fake_ping(host, port, timeout=3.0):
        online = state['survival_online'] if port == port_survival else state['creative_online']
        if not online:
            return PingResult(online=False, error='模拟：端口无人响应')
        return PingResult(online=True, players_online=0, players_max=20, sample=[],
                          version_name='Paper', motd='mock')

    monkeypatch.setattr(supervisor_module, 'status_ping', fake_ping)

    config_a = _config('Survival', bus_a, port_survival)
    config_b = _config('Creative', bus_b, port_creative)
    # 让两边的"对端"指向对方真实的端口
    config_a.servers[1].port, config_a.servers[1].bus = port_creative, f'127.0.0.1:{bus_b}'
    config_a.servers[0].port, config_a.servers[0].bus = port_survival, f'127.0.0.1:{bus_a}'
    config_b.servers[0].port, config_b.servers[0].bus = port_creative, f'127.0.0.1:{bus_b}'
    config_b.servers[1].port, config_b.servers[1].bus = port_survival, f'127.0.0.1:{bus_a}'

    stub_a, stub_b = StubMCDR(), StubMCDR()
    instance_a = Supervisor(stub_a, config_a, plugin_version='test')
    instance_b = Supervisor(stub_b, config_b, plugin_version='test')
    instance_a.start()
    instance_b.start()
    assert wait_until(lambda: instance_a.state_of('Survival').state == State.RUNNING, 10)
    assert wait_until(lambda: instance_b.state_of('Creative').state == State.RUNNING, 10)
    try:
        yield instance_a, instance_b, stub_a, stub_b, (port_survival, port_creative), state
    finally:
        instance_a.shutdown()
        instance_b.shutdown()


# --------------------------------------------------------------------------- #
# 1. 跨实例操作
# --------------------------------------------------------------------------- #

def test_remote_sleep_does_not_answer_itself(two_instances):
    """回归：不能因为"自己回了本实例不负责 X"就把真正的结果盖掉。"""
    instance_a, instance_b, _stub_a, stub_b, (port_survival, _creative), state = two_instances
    state['creative_online'] = 0                       # 让它以为 Creative 的服务端在跑
    ok, detail = instance_a.request_action('sleep', 'Creative', by='测试', wait=False)
    assert ok, detail
    assert '不负责' not in detail, detail               # 这正是线上那句误报
    assert wait_until(lambda: stub_b.stop_calls == 1, 10), 'Creative 的实例并没有真的去休眠'


def test_remote_sleep_waits_for_the_real_result(two_instances):
    """wait=True 时应当等到对方状态真的变了，再回一句真结果。"""
    instance_a, instance_b, _stub_a, _stub_b, (_s, port_creative), state = two_instances
    state['creative_online'] = 0
    ok, detail = instance_a.request_action('sleep', 'Creative', by='测试', wait=True)
    assert ok, detail
    assert '已休眠' in detail or '受理' in detail, detail
    assert instance_b.state_of('Creative').state in (State.SLEEPING, State.OFFLINE)


def test_remote_action_without_bus_address_explains_why(two_instances):
    """没有总线地址时应当明说"没配 bus"，而不是笼统地说没人负责。"""
    instance_a, _instance_b, *_rest = two_instances
    instance_a.config.servers[1].bus = None
    ok, detail = instance_a.request_action('sleep', 'Creative', by='测试', wait=True)
    assert not ok
    assert 'bus' in detail.lower(), detail


def test_action_on_a_server_nobody_manages_explains_the_role(two_instances):
    """谁都不管它时，回复里要指出 role，并告诉用户去改哪里。"""
    instance_a, _instance_b, *_rest = two_instances
    instance_a.config.servers[1].role = 'watch'
    instance_a.config.servers[1].bus = '127.0.0.1:1'   # 指向一个没人监听的地址：等于没人负责
    ok, detail = instance_a.request_action('sleep', 'Creative', by='测试', wait=True)
    assert not ok
    assert 'role' in detail and 'auto' in detail, detail


# --------------------------------------------------------------------------- #
# 2. 假服务端接管端口
# --------------------------------------------------------------------------- #

def test_force_take_over_only_when_no_real_server_answers(monkeypatch):
    """端口上有真实服务端在应答时，绝不允许强行接管（那会把活着的服端口抢走）。"""
    port = free_port()
    config = _config('Survival', free_port(), port)
    config.servers[0].bus = None
    config.fake_server.enabled = True
    stub = StubMCDR()
    instance = Supervisor(stub, config, plugin_version='test')
    try:
        managed = instance.state_of('Survival')

        stub.running = False                           # 前提：MCDR 说服务端已退出
        monkeypatch.setattr(supervisor_module, 'status_ping',
                            lambda *a, **k: PingResult(online=True, players_online=0, players_max=20))
        assert instance._can_force_take_over(managed) is False, '端口上还有真实服务端，不许抢'

        monkeypatch.setattr(supervisor_module, 'status_ping', lambda *a, **k: PingResult(online=False))
        assert instance._can_force_take_over(managed) is True

        stub.running = True                            # MCDR 说服务端还在跑 -> 一律不许
        assert instance._can_force_take_over(managed) is False
    finally:
        instance.shutdown()


def test_sleep_hands_the_port_over_even_if_bind_keeps_failing(two_instances, monkeypatch):
    """回归：绑不上端口时要挂后台重试，而不是等下一个侦察周期（默认 10 分钟）。"""
    instance_a, instance_b, _stub_a, _stub_b, (_s, port_creative), state = two_instances
    managed = instance_b.state_of('Creative')

    calls = {'n': 0}
    real_start = FakeServer.start

    def flaky_start(self, force: bool = False) -> bool:
        calls['n'] += 1
        if calls['n'] <= 4:                            # 前几次模拟"端口还被占着"
            self._last_error = 'OSError: [WinError 10048] address already in use'
            return False
        return real_start(self, force=force)

    monkeypatch.setattr(FakeServer, 'start', flaky_start)
    monkeypatch.setattr(instance_b, '_can_force_take_over', lambda managed: False)

    ok, detail = instance_b.sleep_server('Creative', reason='manual', by='测试')
    assert ok, detail
    assert wait_until(lambda: managed.state == State.SLEEPING, 20), '重试之后仍然没能接管端口'
    assert calls['n'] >= 3, f'只试了 {calls["n"]} 次就放弃了（应当反复重试）'


def test_sleep_replies_promptly_even_when_the_port_stays_stuck(two_instances, monkeypatch):
    """端口一直被占着时，休眠本身要立刻回结果，然后把接管交给后台线程。"""
    _instance_a, instance_b, _stub_a, _stub_b, *_rest = two_instances
    managed = instance_b.state_of('Creative')
    starts = {'n': 0}

    def blocked(self, force: bool = False) -> bool:
        starts['n'] += 1
        self._last_error = 'OSError: [WinError 10048] address already in use'
        return False

    monkeypatch.setattr(FakeServer, 'start', blocked)
    monkeypatch.setattr(instance_b, '_can_force_take_over', lambda managed: False)
    ok, detail = instance_b.sleep_server('Creative', reason='manual', by='测试')
    assert ok and '已休眠' in detail
    assert managed.state == State.OFFLINE
    assert managed.takeover_thread is not None and managed.takeover_thread.is_alive(), '没有挂后台重试'
    assert starts['n'] >= 2

    monkeypatch.undo()
    assert wait_until(lambda: managed.state == State.SLEEPING, 20), '后台重试没能接管端口'


def test_takeover_retry_stops_when_the_server_is_woken(two_instances, monkeypatch):
    """被唤醒后，接管重试线程要自己退出（别在真实服务端启动后再去抢端口）。"""
    _instance_a, instance_b, _stub_a, _stub_b, *_rest = two_instances
    managed = instance_b.state_of('Creative')

    monkeypatch.setattr(FakeServer, 'start', lambda self, force=False: False)
    monkeypatch.setattr(instance_b, '_can_force_take_over', lambda managed: False)
    instance_b._ensure_takeover_retry(managed)
    assert wait_until(lambda: managed.takeover_thread is not None, 3)
    managed.state = State.RUNNING
    assert wait_until(lambda: not managed.takeover_thread.is_alive(), 10), '状态变了还在抢端口'


def test_wake_stops_the_fake_server_and_waits_for_the_port(two_instances):
    """唤醒前要先把假服务端撤下来，否则真实服务端会绑不上端口。"""
    _instance_a, instance_b, _stub_a, stub_b, *_rest = two_instances
    managed = instance_b.state_of('Creative')
    assert instance_b.sleep_server('Creative', reason='manual', by='测试')[0]
    assert managed.fake is not None and managed.fake.running

    stub_b.started_up = False
    assert instance_b.wake_server('Creative', reason='manual', by='测试')[0]
    assert wait_until(lambda: stub_b.start_calls >= 1, 10), (
        '唤醒没有真的去启动服务端：\n' + '\n'.join(f'  [{l}] {t}' for l, t in stub_b.logger.records[-12:]))
    assert not managed.fake.running, '唤醒时没有把假服务端撤下来'
    stub_b.started_up = True
    instance_b.on_server_startup()
    assert wait_until(lambda: managed.state == State.RUNNING, 15)


# --------------------------------------------------------------------------- #
# 6. 谁都没应答时的提示与诊断
# --------------------------------------------------------------------------- #

def test_auto_role_hint_does_not_tell_you_to_set_auto(two_instances):
    """线上反馈：role 本来就是 auto，提示却还在劝"把 role 改成 auto 或 self"，
    照做之后回复一字未变。

    role=auto 的含义是"该由 server.properties 端口与它一致的那台 MCDR 负责"，
    所以这种情形下提示要指向"那台实例没应答"，改 role 是改不出结果的。
    """
    instance_a, _instance_b, *_rest = two_instances
    instance_a.config.servers[1].role = 'auto'
    instance_a.config.servers[1].bus = '127.0.0.1:1'      # 没人监听的地址 = 对方实例不在线
    ok, detail = instance_a.request_action('wake', 'Creative', by='测试', wait=True)
    assert not ok
    assert '改成 auto' not in detail, detail
    assert '在运行' in detail, detail


def test_nobody_answered_is_logged_with_the_address_and_reason(two_instances):
    """一个应答都没有时，控制台要留下"试过哪些地址、各自为什么失败"——
    只有"没有实例负责"这一句在玩家那边，管理员根本无从查起。"""
    instance_a, _instance_b, stub_a, *_rest = two_instances
    instance_a.config.servers[1].role = 'auto'
    instance_a.config.servers[1].bus = '127.0.0.1:1'
    instance_a.request_action('wake', 'Creative', by='测试', wait=True)
    log = stub_a.logger.text()
    assert '没有任何实例应答' in log, log[-800:]
    assert '127.0.0.1:1' in log, log[-800:]


def test_bus_remembers_why_a_send_failed():
    """总线必须留下失败原因（连不上 / 鉴权失败 / 连上了但没应答），否则没法排查。"""
    from idle_guard.bus import Bus
    from mc_test_utils import CollectingLogger

    bus = Bus(None, 'token', lambda message: {'ok': True}, CollectingLogger())
    assert bus.send('127.0.0.1:1', {'type': 'state'}, timeout=0.5) is None
    assert bus.last_error('127.0.0.1:1'), '没有记录失败原因'


def test_bus_keeps_accepting_after_an_unexpected_error(monkeypatch):
    """接受线程遇到意外 OSError 不能直接退出。

    否则 socket 还留在 LISTEN 状态：对端连得上、消息也发得出去，但永远等不到应答，
    在对方看来就是"那台实例不在线"（真实踩过，查了很久）。
    """
    import socket as socket_module

    from idle_guard.bus import Bus
    from mc_test_utils import CollectingLogger

    port = free_port()
    bus = Bus(f'127.0.0.1:{port}', 'token',
              lambda message: {'ok': True, 'echo': message.get('type')},
              CollectingLogger(), timeout=2.0)
    real_accept = socket_module.socket.accept
    calls = {'n': 0}

    def flaky_accept(self):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('模拟一次意外错误')
        return real_accept(self)

    monkeypatch.setattr(socket_module.socket, 'accept', flaky_accept)
    try:
        assert bus.start(), '总线没能启动'
        reply = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and reply is None:
            reply = bus.send(f'127.0.0.1:{port}', {'type': 'state'}, timeout=1.0)
            if reply is None:
                time.sleep(0.5)
        assert reply is not None and reply.get('echo') == 'state', (
            f'接受线程没能恢复（accept 被调用 {calls["n"]} 次）')
    finally:
        bus.stop()
