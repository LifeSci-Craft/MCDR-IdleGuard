"""读路径不该同步等网络；侦察失败要退避。

两条都来自代码审查：

1. **读命令的快照改为"用心跳缓存"**：心跳每 `state_tick` 秒已经在后台问过对端状态并拿回了完整快照
   （以前直接丢掉），于是 `!!ig status/diag/check` 渲染时要在 **MCDR 的命令线程上**再同步问一遍 ——
   每个对端最多等 2 秒，对端被防火墙丢包时能把命令线程卡到十几秒（MCDR 会报 TaskExecutor 无响应）。
   `live_views()` 现在优先用缓存，只为"完全没有缓存"的地址现场问一次。
2. **侦察失败要按整轮间隔退避**：`next_full` 以前只在 `poll_once()` 正常返回后推进，
   持续失败时就会每一轮（默认 5 秒）重试并打一条带堆栈的错误日志。
"""
from __future__ import annotations

import time

import pytest

from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port

PEER_ADDRESS = '127.0.0.1:25601'


def build(*, state_tick: float = 9999.0, check_interval: float = 9999.0):
    """本实例负责 Survival；Creative 由"另一台实例"负责（地址 PEER_ADDRESS）。"""
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=check_interval,
        state_tick=state_tick,
        idle_timeout=9999.0,
        min_uptime=0.0,
        bus=BusConfig(bind='127.0.0.1:25600', token='t', timeout=0.5),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=False, bus='127.0.0.1:25600'),
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                        auto_sleep=False, bus=PEER_ADDRESS),
        ],
    )
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


def peer_servers(state: str = State.SLEEPING.value) -> list:
    """对端应答里那台"它负责的服"。"""
    return [{'name': 'Creative', 'role': 'self', 'state': state, 'players': 0, 'players_max': 20,
             'idle_seconds': -1.0, 'idle_timeout': 3600.0}]


def peer_reply(state: str = State.SLEEPING.value) -> dict:
    return {'ok': True, 'instance': 'Creative', 'servers': peer_servers(state)}


# --------------------------------------------------------------------------- #
# ① 心跳缓存

def test_a_cached_snapshot_is_used_without_touching_the_network():
    supervisor, _stub = build()
    try:
        supervisor._peer_snapshots[PEER_ADDRESS] = {
            'at': time.monotonic(), 'instance': 'Creative',
            'servers': peer_servers(), 'fresh_probe': False,
        }
        calls: list = []
        supervisor._bus.broadcast = lambda *a, **k: calls.append(a) or []
        views, live = supervisor.live_views()
        assert views['Creative'].state == State.SLEEPING.value, '没有采用缓存里的对端快照'
        assert 'Creative' in live
        assert calls == [], '有可用缓存时不该再同步广播（那会阻塞命令线程）'
    finally:
        supervisor.shutdown()


def test_the_heartbeat_caches_what_it_brings_back():
    supervisor, _stub = build()
    try:
        supervisor._bus.send = lambda address, message, timeout=None: peer_reply()
        supervisor._poll_peers()
        snapshot = supervisor._peer_snapshots[PEER_ADDRESS]
        assert snapshot['servers'], '心跳拿到的快照没有被留下'
        assert snapshot['fresh_probe'] is False, '缓存里的旧快照不该冒充"刚刚通过话"'
        assert supervisor.peer_health()[PEER_ADDRESS]['instance'] == 'Creative'
    finally:
        supervisor.shutdown()


def test_an_unreachable_peer_is_remembered_so_the_read_path_never_waits():
    """对端不在线时也要留下"刚问过"的标记：否则读命令会为它再等满一次超时。"""
    supervisor, _stub = build()
    try:
        supervisor._bus.send = lambda address, message, timeout=None: None
        supervisor._poll_peers()
        assert supervisor._peer_snapshots[PEER_ADDRESS]['servers'] == []
        calls: list = []
        supervisor._bus.broadcast = lambda *a, **k: calls.append(a) or []
        views, live = supervisor.live_views()
        assert calls == [], '刚问过且不在线的对端，不该在读命令里再等一次'
        assert 'Creative' not in live, '对端没应答时它的数据不该被当成可信'
        assert views['Creative'].state != State.SLEEPING.value
    finally:
        supervisor.shutdown()


def test_nothing_cached_yet_falls_back_to_one_probe():
    """完全没缓存（插件刚起来 / 关掉了心跳）时才现场问一次。"""
    supervisor, _stub = build(state_tick=0)
    try:
        calls: list = []

        def fake_broadcast(addresses, message, timeout=None):
            calls.append(list(addresses))
            return [peer_reply()]

        supervisor._bus.broadcast = fake_broadcast
        views, live = supervisor.live_views()
        assert calls == [[PEER_ADDRESS]], calls
        assert views['Creative'].state == State.SLEEPING.value
        assert 'Creative' in live
    finally:
        supervisor.shutdown()


def test_a_stale_cache_is_refreshed_instead_of_being_trusted():
    """缓存过期（心跳停了 / 对端很久没应答）就退回现场探测，不能一直拿旧数据当真。"""
    supervisor, _stub = build()
    try:
        supervisor._peer_snapshots[PEER_ADDRESS] = {
            'at': time.monotonic() - 10_000.0, 'instance': 'Creative',
            'servers': peer_servers(State.SLEEPING.value), 'fresh_probe': False,
        }
        calls: list = []
        supervisor._bus.broadcast = (lambda addresses, message, timeout=None:
                                     (calls.append(list(addresses)), [peer_reply(State.RUNNING.value)])[1])
        views, _live = supervisor.live_views()
        assert calls == [[PEER_ADDRESS]], '过期缓存应当被现场探测替换'
        assert views['Creative'].state == State.RUNNING.value
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 侦察失败的退避

def test_a_failing_poll_backs_off_instead_of_retrying_every_tick(monkeypatch):
    supervisor, stub = build(state_tick=0.2, check_interval=30.0)
    counts = {'poll': 0, 'tick': 0}

    def boom() -> None:
        counts['poll'] += 1
        raise RuntimeError('模拟持续失败')

    monkeypatch.setattr(supervisor, 'poll_once', boom)
    monkeypatch.setattr(supervisor, '_tick_states',
                        lambda: counts.__setitem__('tick', counts['tick'] + 1))
    try:
        supervisor.start()
        time.sleep(3.6)                     # 轮询循环开头会先等 2 秒
        assert counts['poll'] == 1, f'失败后没有退避，重试了 {counts["poll"]} 次'
        assert counts['tick'] >= 2, f'心跳没有照常跑：{counts}'
        assert '轮询时出现异常' in stub.logger.text()
    finally:
        supervisor.shutdown()
