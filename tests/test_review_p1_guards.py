"""P1 批次（安全 / 资源 / 正确性）的回归测试。

四条都来自代码审查，共同点是"一条坏输入能把整轮或整台实例搞死"：

1. **总线对端给的 `sound` 直接拼进服务端控制台命令**（IG-A-01）：MCDR 的 `send()` 是
   `(text + "\\n")` 写服务端 stdin，所以值里一个换行就等于第二条控制台命令；
2. **共享探测池被对端探测占满后 `_ping_all` 超时炸掉整轮侦察**（IG-A-02）；
3. **servers[].port 越界抛 OverflowError**，把整轮侦察 / 接管线程一起打断（IG-B-03）；
4. **空口令 = 没有鉴权**，任何能连上端口的人都能远程休眠 / 唤醒（IG-B-06）。
"""
from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from idle_guard import supervisor as sup_mod
from idle_guard.bus import Bus
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.fakeserver import FakeServer
from idle_guard.mcproto import PingResult, bind_error, port_open, status_ping
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import CollectingLogger, StubMCDR, free_port

PEER_ADDRESS = '127.0.0.1:25601'


def build(servers=None, *, bus_token: str = 't', fake_server: bool = False, **kwargs):
    """本实例负责 Survival；Creative 由"另一台实例"负责（地址 PEER_ADDRESS）。"""
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=9999.0,
        idle_timeout=9999.0,
        min_uptime=0.0,
        bus=BusConfig(bind=None, token=bus_token, timeout=0.5),
        fake_server=FakeServerConfig(enabled=fake_server),
        announce=AnnounceConfig(enabled=False),
        servers=servers if servers is not None else [
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=False, bus='127.0.0.1:25600'),
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                        auto_sleep=False, bus=PEER_ADDRESS),
        ],
        **kwargs,
    )
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


# --------------------------------------------------------------------------- #
# IG-A-01 总线对端提供的 sound / player 必须先校验
# --------------------------------------------------------------------------- #

#: 每一个都曾经能被原样拼成 `... playsound <值> master ...` 交给服务端控制台
ILLEGAL_SOUNDS = [
    'minecraft:block.note_block.pling\nsay pwned',      # 换行 = 第二条控制台命令
    'minecraft:block.note_block.pling\r\nstop',
    'minecraft:block.note_block.pling\n',
    'minecraft:block.note_block.pling;stop',
    'minecraft:block.note_block.pling stop',            # 空格
    ' minecraft:block.note_block.pling',                # 首尾空白也算（刻意不 strip）
    'minecraft:block.note_block.pling ',
    'minecraft:block.note_block.pling\u0007',           # 控制字符
    'a' * 65,                                            # 超长
    'Minecraft:Block.Note_Block.Pling',                 # 大写不是合法资源位置
    '',
]


@pytest.mark.parametrize('sound', ILLEGAL_SOUNDS)
def test_a_bus_sound_message_with_an_illegal_sound_never_reaches_the_console(sound):
    supervisor, stub = build()
    try:
        supervisor.on_player_joined('Steve')        # 玩家在场：否则本来就不会发命令
        reply = supervisor._on_bus_message({'type': 'announce', 'kind': 'sound',
                                            'player': 'Steve', 'sound': sound})
        assert reply == {'ok': True, 'delivered': False}
        assert stub.executed == [], f'非法音效被拼进了控制台命令：{stub.executed}'
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize('player', [
    'Steve\nstop', 'Steve] run op @a', 'bad name', 'x' * 17, '',
])
def test_a_bus_sound_message_with_an_illegal_player_never_reaches_the_console(player):
    supervisor, stub = build()
    try:
        supervisor.on_player_joined('Steve')
        reply = supervisor._on_bus_message({'type': 'announce', 'kind': 'sound',
                                            'player': player,
                                            'sound': 'minecraft:block.note_block.pling'})
        assert reply == {'ok': True, 'delivered': False}
        assert stub.executed == [], f'非法玩家名被拼进了控制台命令：{stub.executed}'
    finally:
        supervisor.shutdown()


def test_local_play_sound_also_rejects_an_illegal_sound_id():
    """纵深防御：本地路径（_play_local）自己也要挡一道。"""
    supervisor, stub = build()
    try:
        supervisor.on_player_joined('Steve')
        supervisor.play_sound('Steve', 'minecraft:block.note_block.pling\nstop')
        assert stub.executed == []
        assert '不是合法的音效 id' in stub.logger.text(), stub.logger.text()
    finally:
        supervisor.shutdown()


def test_a_legal_sound_still_reaches_the_console():
    """守卫不能把正常音效一起挡掉。"""
    supervisor, stub = build()
    try:
        supervisor.on_player_joined('Steve')
        supervisor.play_sound('Steve', 'minecraft:block.note_block.pling')
        assert any('execute at Steve run playsound minecraft:block.note_block.pling master Steve'
                   in command for command in stub.executed), stub.executed
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize('sound,expected', [
    ('minecraft:block.note_block.pling', 'minecraft:block.note_block.pling'),
    ('my_pack:custom.sound-1', 'my_pack:custom.sound-1'),
    ('a' * 64, 'a' * 64),
    ('', ''),
    (None, ''),
    ('a' * 65, ''),
    ('minecraft:block note', ''),
    ('minecraft:block\nsay x', ''),
    ('[minecraft:x]', ''),
])
def test_safe_sound_id(sound, expected):
    assert sup_mod.safe_sound_id(sound) == expected


# --------------------------------------------------------------------------- #
# IG-A-02 探测池饥饿：保住已收到的结果，并给对端探测去重 / 分池
# --------------------------------------------------------------------------- #

def test_ping_all_keeps_partial_results_when_the_pool_is_saturated(monkeypatch):
    """池被占满时 `as_completed` 会超时：以前整轮侦察（含假服务端维护、空闲判定）全丢。"""
    entries = [
        ServerEntry(name=f'watch{i}', host='127.0.0.1', port=free_port(), role='watch',
                    auto_sleep=False)
        for i in range(3)
    ]
    supervisor, stub = build(servers=entries)
    try:
        # 只留一个 worker，每个探测 0.8 秒，预算 1.0 秒：第三个必然跑不完
        supervisor._pool.shutdown(wait=False)
        supervisor._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(sup_mod, '_ping_budget', lambda count, **kwargs: 1.0)

        def slow_ping(host, port, timeout=3.0):
            time.sleep(0.8)
            return PingResult(online=True, players_online=0, players_max=20)

        monkeypatch.setattr(sup_mod, 'status_ping', slow_ping)

        results = supervisor._ping_all()          # 修复前：这里抛 TimeoutError
        assert len(results) == 1, f'已收到的结果被丢掉了：{sorted(results)}'
        assert all(ping.online for ping in results.values())
        assert '未在' in stub.logger.text(), stub.logger.text()

        time.sleep(1.8)                           # 让上一轮遗留的探测跑完，下一轮时序才确定
        supervisor.poll_once()                    # 修复前：整轮侦察在这里中断
        assert set(supervisor._views) == {f'watch{i}' for i in range(3)}, '本轮没有刷新全部子服'
        finished = [name for name, view in supervisor._views.items()
                    if view.state == State.RUNNING.value]
        assert finished, '已经拿到结果的那台没有被采纳'
    finally:
        supervisor.shutdown()


def test_only_one_peer_poll_runs_at_a_time(monkeypatch):
    """1 秒去抖挡不住"上一批还没跑完又来一批"，必须有 in-flight 去重。"""
    supervisor, _stub = build()
    try:
        started = threading.Event()
        release = threading.Event()
        calls: list = []

        def slow_broadcast(addresses, message, timeout=None):
            calls.append(list(addresses))
            started.set()
            release.wait(5)
            return {}

        monkeypatch.setattr(supervisor._bus, 'broadcast_map', slow_broadcast)
        threads = [threading.Thread(target=supervisor._poll_peers) for _ in range(8)]
        for thread in threads:
            thread.start()
        assert started.wait(5), '对端探测没有跑起来'
        time.sleep(0.2)
        release.set()
        for thread in threads:
            thread.join(5)
        assert len(calls) == 1, f'同时提交了 {len(calls)} 次对端探测（缺少 in-flight 去重）'

        # 去重是"同时只跑一个"，不是"只跑一次"：上一批结束后还能再补刷
        supervisor._poll_peers()
        assert len(calls) == 2, 'in-flight 标记没有在结束时放开'
    finally:
        supervisor.shutdown()


def test_peer_poll_dedup_is_released_after_a_failure(monkeypatch):
    """失败也必须把名额放回去，否则对端健康度再也不更新。"""
    supervisor, _stub = build()
    try:
        def boom(addresses, message, timeout=None):
            raise RuntimeError('模拟广播炸了')

        monkeypatch.setattr(supervisor._bus, 'broadcast_map', boom)
        with pytest.raises(RuntimeError):
            supervisor._poll_peers()

        seen: list = []
        monkeypatch.setattr(supervisor._bus, 'broadcast_map',
                            lambda addresses, message, timeout=None: seen.append(1) or {})
        supervisor._poll_peers()
        assert seen == [1], '探测失败后 in-flight 名额没有释放'
    finally:
        supervisor.shutdown()


def test_peer_probe_runs_in_its_own_pool(monkeypatch):
    """对端探测与状态探测分池：否则对端一多就把状态查询饿死。"""
    supervisor, _stub = build()
    try:
        assert supervisor._pool is not supervisor._peer_pool

        def boom():
            raise AssertionError('对端探测不该占用状态探测池')

        monkeypatch.setattr(supervisor._pool, 'submit', lambda *a, **k: boom())
        seen: list = []
        monkeypatch.setattr(supervisor._peer_pool, 'submit', lambda fn, *a: seen.append(fn))
        supervisor._last_peer_refresh = 0.0
        supervisor.note_peer_state_change()
        assert seen == [supervisor._poll_peers], '补刷没有走对端探测池'
    finally:
        supervisor.shutdown()


def test_tick_states_does_not_block_on_peer_probes(monkeypatch):
    """心跳里的对端探测改成后台提交：以前它同步串行等 1.5 秒 × 对端数。"""
    supervisor, _stub = build()
    try:
        monkeypatch.setattr(supervisor, '_poll_peers', lambda: time.sleep(2.0))
        started = time.monotonic()
        supervisor._tick_states()
        elapsed = time.monotonic() - started
        assert elapsed < 1.9, f'心跳被对端探测同步拖住了 {elapsed:.2f} 秒'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-03 端口越界：最多表现为"这一条不可用"，不打断整轮
# --------------------------------------------------------------------------- #

def test_an_out_of_range_port_never_raises_out_of_the_probe_helpers():
    assert port_open('127.0.0.1', 999999, 0.2) is False
    assert status_ping('127.0.0.1', 999999, 0.2).online is False
    error = bind_error('0.0.0.0', 999999)          # 修复前：这里抛 OverflowError
    assert isinstance(error, OverflowError), error


def test_fake_server_start_with_an_out_of_range_port_returns_false():
    """文档承诺"端口不可用时返回 False、不抛异常" —— 越界端口也算不可用。"""
    fake = FakeServer('0.0.0.0', 999999, lambda protocol: {}, lambda: 'x',
                      lambda player, peer: None, CollectingLogger())
    assert fake.start() is False
    assert 'Overflow' in fake.last_error, fake.last_error
    assert fake.running is False


def test_poll_once_survives_an_entry_with_an_out_of_range_port():
    entries = [
        ServerEntry(name='broken', host='127.0.0.1', port=999999, role='command',
                    start_command='true', auto_sleep=False),
        ServerEntry(name='fine', host='127.0.0.1', port=free_port(), role='watch',
                    auto_sleep=False),
    ]
    supervisor, _stub = build(servers=entries, fake_server=True)
    try:
        supervisor.poll_once()          # 修复前：这里抛 OverflowError，后面的条目全没处理
        assert set(supervisor._views) == {'broken', 'fine'}, '排在坏条目后面的子服没被刷新'
    finally:
        supervisor.shutdown()


def test_check_reports_an_out_of_range_port_and_an_empty_host():
    entries = [ServerEntry(name='bad', host='', port=999999, role='watch', auto_sleep=False)]
    supervisor, _stub = build(servers=entries)
    try:
        text = '\n'.join(supervisor.config_warnings())
        assert 'port=999999' in text, text
        assert 'host 是空的' in text, text
    finally:
        supervisor.shutdown()


def test_check_does_not_complain_about_a_normal_port():
    supervisor, _stub = build()
    try:
        text = '\n'.join(supervisor.config_warnings())
        assert '65535' not in text, text
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-06 空口令 = 没有鉴权
# --------------------------------------------------------------------------- #

def test_a_bus_with_an_empty_token_refuses_to_listen():
    port = free_port()
    logger = CollectingLogger()
    bus = Bus(f'127.0.0.1:{port}', '', lambda message: {'ok': True}, logger)
    try:
        assert bus.start() is False, '空口令下总线上线了'
        assert bus.running is False
        assert '口令' in logger.text(), logger.text()
        # 端口上不该有监听者
        assert port_open('127.0.0.1', port, 0.3) is False
    finally:
        bus.stop()


def test_a_whitespace_only_token_also_refuses_to_listen():
    port = free_port()
    logger = CollectingLogger()
    bus = Bus(f'127.0.0.1:{port}', '   ', lambda message: {'ok': True}, logger)
    try:
        assert bus.start() is False
        assert port_open('127.0.0.1', port, 0.3) is False
    finally:
        bus.stop()


def test_a_bus_with_a_real_token_still_starts():
    port = free_port()
    bus = Bus(f'127.0.0.1:{port}', 'secret', lambda message: {'ok': True}, CollectingLogger())
    try:
        assert bus.start() is True
    finally:
        bus.stop()


@pytest.mark.parametrize('token,needle', [
    ('', '空的'),
    ('   ', '空的'),
    ('idle_guard', '老默认值'),
    ('CHANGE_ME_please', '占位串'),
])
def test_check_names_a_bad_bus_token(token, needle):
    supervisor, _stub = build(bus_token=token)
    try:
        text = '\n'.join(supervisor.config_warnings())
        assert needle in text, (token, text)
    finally:
        supervisor.shutdown()


def test_check_says_nothing_when_the_token_is_fine():
    supervisor, _stub = build(bus_token='a-proper-random-token')
    try:
        assert supervisor.config_warnings() == []
    finally:
        supervisor.shutdown()
