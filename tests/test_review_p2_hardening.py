"""P2 批次（并发 / 资源 / 错误处理）的回归测试。

覆盖的十条：

- IG-A-03：Stats 的锁要罩住 `_entry()` / `snapshot()`，写盘前先在锁内浅拷贝；
- IG-A-04：`attempts` / `pending_greetings` / `_recent` 的读写都要有锁或快照；
- IG-A-05：`startup_seconds.json` 原子写 + 坏文件隔离；
- IG-A-06：外部唤醒（控制台开服 / role=command 状态转 RUNNING）也要结算休眠时长；
- IG-A-08：role=command 的子服要写 `last_stopped_at`（冷 / 热启动分桶靠它）；
- IG-B-01：`status_ping` 绝不抛异常 + 唤醒线程体兜住所有异常；
- IG-B-02：时间类配置的范围校验（0 / 负数 / inf / nan / 超大）；
- IG-B-04：接受循环在 `Thread.start` 失败后仍然活着并回滚名额；
- IG-B-05：假服务端等对端关闭要有**总**预算，细水长流的客户端必须被放掉。
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import pytest

from idle_guard import supervisor as sup_mod
from idle_guard.config import (
    NUMERIC_LIMITS, AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry,
    sanitize_numbers,
)
from idle_guard.fakeserver import FakeServer
from idle_guard.mcproto import (
    ConnReader, PingResult, encode_string, encode_ushort, encode_varint, pack_packet,
    status_ping,
)
from idle_guard.models import ManagedServer, State
from idle_guard.stats import Stats
from idle_guard.supervisor import Supervisor
from mc_test_utils import CollectingLogger, StubMCDR, free_port, wait_until


def build(servers=None, *, data_dir=None, **kwargs):
    """本实例负责 Survival（单条、不配总线，避免并发测试里真的去连对端）。"""
    settings = {
        'instance_name': 'Survival',
        'language': 'zh_cn',
        'check_interval': 9999.0,
        'state_tick': 9999.0,
        'idle_timeout': 9999.0,
        'min_uptime': 0.0,
        'startup_timeout': 1.0,
        'stop_timeout': 1.0,
        'port_release_timeout': 0.6,
        'wake_cooldown': 0.0,
        'bus': BusConfig(bind=None, token='t', timeout=0.3),
        'fake_server': FakeServerConfig(enabled=False),
        'announce': AnnounceConfig(enabled=False),
        'servers': servers if servers is not None else [
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=False),
        ],
    }
    settings.update(kwargs)          # 让用例可以覆盖任意字段（含 check_interval=0 这类坏值）
    stub = StubMCDR()
    if data_dir is not None:
        stub.data_folder = str(data_dir)
    return Supervisor(stub, Config(**settings), plugin_version='test'), stub


def command_server(name: str = 'Cmd', **kwargs) -> ServerEntry:
    return ServerEntry(name=name, host='127.0.0.1', port=free_port(), role='command',
                       start_command='true', stop_command='true', auto_sleep=False, **kwargs)


# --------------------------------------------------------------------------- #
# IG-A-03 / IG-A-04 容器并发
# --------------------------------------------------------------------------- #

def _join(threads, timeout: float = 20.0) -> None:
    for thread in threads:
        thread.join(timeout)


def test_stats_snapshot_survives_a_growing_container():
    """`!!ig stats` 渲染（snapshot）与动作线程插入新名字（_entry）会撞在一起。"""
    stats = Stats(None)
    names = [f'w{index}' for index in range(20000)]
    errors: list = []
    started = threading.Event()

    def writer():
        started.wait(5)
        for name in names:
            stats._entry(name)['sleep'] += 1

    def reader():
        started.wait(5)
        for _ in range(200):
            try:
                stats.snapshot()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    writers = [threading.Thread(target=writer) for _ in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in writers + readers:
        thread.start()
    started.set()
    _join(writers + readers)
    assert errors == [], f'并发下 snapshot() 抛异常：{errors[:3]}'


def test_snapshot_returns_a_copy_not_the_live_dict():
    stats = Stats(None)
    stats.note_sleep('a')
    _since, data = stats.snapshot()
    data['a']['sleep'] = 999
    _since, again = stats.snapshot()
    assert again['a']['sleep'] == 1.0, 'snapshot 返回的不是拷贝'


def test_record_attempt_is_safe_under_concurrent_logins():
    managed = ManagedServer(ServerEntry(name='A', host='127.0.0.1', port=1), 'self')
    errors: list = []

    def writer(offset: int):
        for index in range(4000):
            # window 很小 → 每次调用都会插入 + 立刻清理，字典尺寸持续变化（最容易撞上迭代器）
            managed.record_attempt(f'p{offset}_{index}', time.monotonic(), 0.001)

    def reader():
        for _ in range(4000):
            try:
                managed.attempt_players(time.monotonic(), 60.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    _join(threads)
    assert errors == [], f'并发 record_attempt / attempt_players 抛异常：{errors[:3]}'


def test_pending_greetings_are_safe_under_concurrency():
    managed = ManagedServer(ServerEntry(name='A', host='127.0.0.1', port=1), 'self')
    errors: list = []

    def writer(offset: int):
        for index in range(3000):
            managed.remember_greeting(f'p{offset}_{index}', time.monotonic() - 1.0)

    def reader():
        for _ in range(3000):
            try:
                managed.drop_expired_greetings(time.monotonic())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    _join(threads)
    assert errors == [], f'并发 pending_greetings 抛异常：{errors[:3]}'


def test_recent_events_survive_concurrent_state_flips():
    """`!!ig diag` 的"最近事件"以前会被并发 append 打成 "deque mutated during iteration"。"""
    supervisor, _stub = build()
    try:
        managed = supervisor.state_of('Survival')
        errors: list = []
        stop = threading.Event()

        def flipper():
            while not stop.is_set():
                supervisor._set_state(managed, State.RUNNING, '')
                supervisor._set_state(managed, State.SLEEPING, '')

        def reader():
            while not stop.is_set():
                try:
                    supervisor.recent_events()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=flipper) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        stop.set()
        _join(threads)
        assert errors == [], f'并发读 recent_events 抛异常：{errors[:3]}'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-05 startup_seconds.json 的原子写与坏文件隔离
# --------------------------------------------------------------------------- #

def test_startup_seconds_are_written_atomically(tmp_path):
    """每个 _do_wake 线程都会写同一个文件：以前是 `open('w') + json.dump`，会互相截断。"""
    supervisor, _stub = build(data_dir=tmp_path)
    path = tmp_path / 'startup_seconds.json'

    # ① 确定性证据：写到一半失败时，**原来那份文件必须完好**。
    #    直接 `open('w')` 会先把文件截断，这里就会读到半截 JSON。
    supervisor._remember_startup_seconds('keep', 7.5, 10.0)
    with open(path, encoding='utf-8') as handle:
        before = json.load(handle)
    assert before

    def broken_dump(payload, handle, **kwargs):
        handle.write('{"half": ')          # 先落下半截，再炸
        raise OSError('模拟磁盘写入中途失败')

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sup_mod.json, 'dump', broken_dump)
        supervisor._remember_startup_seconds('other', 9.0, 10.0)
    with open(path, encoding='utf-8') as handle:
        assert json.load(handle) == before, '写到一半失败把原文件毁了（不是原子写）'
    assert '启动耗时记录写入失败' in _stub.logger.text()

    # ② 并发写：合并后必须是一份完整、可解析、包含全部写入者的 JSON
    errors: list = []

    def writer(name: str):
        for index in range(30):
            try:
                supervisor._remember_startup_seconds(name, 1.0 + index * 0.1, 10.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    writers = [threading.Thread(target=writer, args=(f's{index}',)) for index in range(6)]
    for thread in writers:
        thread.start()
    _join(writers)
    assert errors == [], f'并发写 startup_seconds.json 出问题：{errors[:3]}'
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    assert {f's{index}' for index in range(6)} <= set(data)
    assert data['keep'] == before['keep']


def test_a_corrupt_startup_file_is_quarantined(tmp_path):
    (tmp_path / 'startup_seconds.json').write_text('{"a": [[1.0, 2.0]]}{"b": 3}', encoding='utf-8')
    supervisor, _stub = build(data_dir=tmp_path)
    try:
        assert supervisor.startup_samples('a') == []
        assert (tmp_path / 'startup_seconds.json.corrupt').exists(), '坏文件没有被隔离留证'
        assert not (tmp_path / 'startup_seconds.json').exists()
        assert '启动耗时记录读取失败' in _stub.logger.text()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-06 外部唤醒也要结算休眠时长
# --------------------------------------------------------------------------- #

def test_an_external_start_settles_the_sleep_seconds():
    supervisor, _stub = build()
    try:
        managed = supervisor.state_of('Survival')
        managed.state = State.SLEEPING
        supervisor._stats.note_sleep('Survival')
        time.sleep(0.05)
        supervisor.on_server_startup()          # 管理员从 MCDR 控制台 / 脚本开服
        _since, data = supervisor.stats_snapshot()
        settled = data['Survival']['sleep_seconds']
        assert settled > 0, '外部唤醒没有结算休眠时长（_sleeping_since 没清）'
        assert data['Survival']['wake'] == 0.0, 'settle() 不该计一次唤醒'
        time.sleep(0.12)
        _since, data = supervisor.stats_snapshot()
        assert data['Survival']['sleep_seconds'] == settled, '结算之后仍在累加这一段休眠'
    finally:
        supervisor.shutdown()


def test_a_command_server_that_turns_running_settles_the_sleep_seconds():
    supervisor, _stub = build(servers=[command_server()])
    try:
        managed = supervisor.state_of('Cmd')
        supervisor._stats.note_sleep('Cmd')
        managed.state = State.SLEEPING
        time.sleep(0.05)
        supervisor._update_managed(managed, PingResult(online=True, players_online=0, players_max=20))
        assert managed.state == State.RUNNING
        _since, data = supervisor.stats_snapshot()
        assert data['Cmd']['sleep_seconds'] > 0, 'role=command 转 RUNNING 时没有结算'
    finally:
        supervisor.shutdown()


def test_reconcile_starts_timing_for_a_server_that_is_already_asleep():
    """插件重载会重建 Stats：正在睡的那台服要能重新开始计时，而不是永远 0。"""
    supervisor, _stub = build()
    try:
        managed = supervisor.state_of('Survival')
        managed.state = State.SLEEPING
        supervisor._reconcile_sleep_stats()
        time.sleep(0.05)
        _since, data = supervisor.stats_snapshot()
        assert data['Survival']['sleep_seconds'] > 0, '正在睡的服没有开始计时'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-08 role=command 的停机时刻
# --------------------------------------------------------------------------- #

def test_sleeping_a_command_server_records_the_stop_time():
    supervisor, _stub = build(servers=[command_server()])
    try:
        managed = supervisor.state_of('Cmd')
        managed.state = State.RUNNING
        managed.slp = PingResult(online=True, players_online=0, players_max=20)
        ok, detail = supervisor.sleep_server('Cmd', reason='manual', by='console')
        assert ok, detail
        assert managed.last_stopped_at is not None, 'role=command 停机后没记 last_stopped_at'
        assert supervisor._down_seconds(managed) is not None
    finally:
        supervisor.shutdown()


def test_a_command_server_that_goes_offline_records_the_stop_time():
    """外部停服（不是本实例发的停止命令）同样要记账。"""
    supervisor, _stub = build(servers=[command_server()])
    try:
        managed = supervisor.state_of('Cmd')
        managed.state = State.RUNNING
        supervisor._update_managed(managed, PingResult(online=False))
        assert managed.state == State.OFFLINE
        assert managed.last_stopped_at is not None
        assert supervisor._down_seconds(managed) is not None
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-01 status_ping 的"绝不抛异常"与唤醒线程体
# --------------------------------------------------------------------------- #

#: 每一种都曾经让 status_ping 抛出 except 列表之外的异常
MALFORMED_STATUS_PAYLOADS = [
    '{"players": {"online": null, "max": null}, "version": {"protocol": null}}',
    '{"players": {"online": {}, "max": []}}',
    '{"players": {"sample": 5}}',
    '{"players": {"sample": {"name": "x"}}}',
    '{"version": null, "description": "ok"}',
    '{"description": ' + '{"text": ' * 1500 + '"deep"' + '}' * 1500 + '}',
    'this is not json',
    '[]',
]


@contextlib.contextmanager
def crafted_status_server(json_text: str):
    """一个只会应答一次状态查询的迷你服务端（回一段我们指定的报文）。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    server.listen(4)
    port = server.getsockname()[1]

    def serve():
        try:
            conn, _addr = server.accept()
            with conn:
                reader = ConnReader(conn)
                reader.read_packet()          # handshake
                reader.read_packet()          # status request
                conn.sendall(pack_packet(0x00, encode_string(json_text)))
        except Exception:  # noqa: BLE001 - 迷你服务端不该影响测试结论
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.close()
        thread.join(2.0)


@pytest.mark.parametrize('json_text', MALFORMED_STATUS_PAYLOADS)
def test_status_ping_never_raises_on_a_malformed_payload(json_text):
    with crafted_status_server(json_text) as port:
        result = status_ping('127.0.0.1', port, 2.0)      # 修复前：TypeError / RecursionError
    assert isinstance(result, PingResult)
    assert isinstance(result.players_online, int)
    assert isinstance(result.protocol, int)


def test_a_thread_body_failure_cannot_leave_the_state_machine_stuck(monkeypatch):
    """唤醒线程体抛异常时：必须记 error 并把状态放回可用状态，而不是停在 STARTING。"""
    supervisor, stub = build()
    try:
        managed = supervisor.state_of('Survival')

        def boom(*args, **kwargs):
            raise RuntimeError('模拟唤醒过程中炸了')

        monkeypatch.setattr(supervisor, '_do_wake_inner', boom)
        supervisor._set_state(managed, State.STARTING, 'wake')
        supervisor._do_wake(managed, 'manual', 'console', None)
        assert managed.state == State.OFFLINE, '唤醒线程抛异常后状态卡在了 STARTING'
        assert '未预期异常' in stub.logger.text(), stub.logger.text()
        assert managed.wake_failures == 1
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-02 时间类配置的范围校验
# --------------------------------------------------------------------------- #

#: 顶层的时间类字段（`bus.timeout` 这种子对象字段由 test_sanitize_numbers_reports_what_it_changed 覆盖）
_TIME_FIELDS = sorted(name for name in NUMERIC_LIMITS if '.' not in name and name != 'max_wake_failures')


@pytest.mark.parametrize('field', _TIME_FIELDS)
@pytest.mark.parametrize('bad', [0, -5, float('inf'), float('-inf'), float('nan'), 1e12])
def test_bad_numeric_values_fall_back_to_the_default(field, bad):
    """0 / 负数 / inf / nan / 超大：非法的一律退回默认值，合法的（例如 min_uptime=0）原样保留。"""
    low, high, default = NUMERIC_LIMITS[field]
    supervisor, stub = build(**{field: bad})
    try:
        current = getattr(supervisor.config, field)
        legal = low <= bad <= high
        if legal:
            assert current == bad, f'{field} 的合法值被改掉了：{current!r}'
        else:
            assert current == default, f'{field}={bad!r} 没有退回默认值（现在是 {current!r}）'
            assert '不在允许范围内' in stub.logger.text(), (field, stub.logger.text())
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize('bad', [0, -1, 1e12, float('inf'), float('nan')])
def test_max_wake_failures_falls_back_to_the_default(bad):
    supervisor, stub = build(max_wake_failures=bad)
    try:
        assert supervisor.config.max_wake_failures == 3
        assert '不在允许范围内' in stub.logger.text()
    finally:
        supervisor.shutdown()


def test_sanitize_numbers_reports_what_it_changed():
    config = Config(check_interval=0.0, stop_timeout=float('inf'), bus=BusConfig(timeout=0.0))
    changed = {name: (value, default) for name, value, default in sanitize_numbers(config)}
    assert set(changed) == {'check_interval', 'stop_timeout', 'bus.timeout'}
    assert config.check_interval == 600.0
    assert config.stop_timeout == 180.0
    assert config.bus.timeout == 3.0


def test_an_infinite_fallback_startup_seconds_does_not_break_the_eta():
    supervisor, _stub = build(fallback_startup_seconds=float('inf'))
    try:
        assert supervisor.config.fallback_startup_seconds == 60.0
        assert int(supervisor.eta_of('Survival')) == 60     # 修复前：int(inf) 抛 OverflowError
    finally:
        supervisor.shutdown()


def test_the_poll_loop_does_not_busy_spin_with_a_zero_check_interval(monkeypatch):
    """state_tick=0 + check_interval=0 曾经 1 秒跑 135 万次 poll_once。"""
    supervisor, _stub = build(state_tick=0.0, check_interval=0.0)
    counts = {'poll': 0}

    def counting_poll():
        counts['poll'] += 1

    monkeypatch.setattr(supervisor, 'poll_once', counting_poll)
    monkeypatch.setattr(supervisor, '_tick_states', lambda: None)
    try:
        assert supervisor.config.check_interval == 600.0
        supervisor.start()
        time.sleep(2.6)          # _poll_loop 启动后会先等 2 秒
        assert counts['poll'] <= 5, f'轮询线程忙转了 {counts["poll"]} 次'
    finally:
        supervisor.shutdown()


def test_check_reports_a_bad_numeric_value():
    supervisor, _stub = build(check_interval=0.0)
    try:
        text = '\n'.join(supervisor.config_warnings())
        assert 'check_interval' in text, text
    finally:
        supervisor.shutdown()


def test_check_reports_an_illegal_role():
    """非法 role 会被归一化成 auto，但自检必须点名（否则 diag 里会打出原始键名）。"""
    entries = [ServerEntry(name='Weird', host='127.0.0.1', port=free_port(),
                           role='nonsense', auto_sleep=False)]
    supervisor, _stub = build(servers=entries)
    try:
        text = '\n'.join(supervisor.config_warnings())
        assert 'nonsense' in text, text
        view = next(view for view in supervisor.local_views() if view.name == 'Weird')
        assert view.role in ('watch', 'self', 'command'), f'diag 会打出原始键名：{view.role}'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-04 接受循环的自愈（Thread.start 失败）
# --------------------------------------------------------------------------- #

class _BrokenThread:
    """模拟资源紧张时的 `RuntimeError: can't start new thread`。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("can't start new thread")


def test_the_bus_accept_loop_survives_a_failed_thread_start(monkeypatch):
    from idle_guard.bus import Bus

    port = free_port()
    bus = Bus(f'127.0.0.1:{port}', 'tok', lambda message: {'ok': True}, CollectingLogger())
    assert bus.start()
    try:
        monkeypatch.setattr(threading, 'Thread', _BrokenThread)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                pass
            assert wait_until(lambda: bus._active == 0, 3), '失败后名额没有回滚'
        finally:
            monkeypatch.undo()
        assert bus._thread.is_alive(), 'Thread.start 失败把接受线程终结了'

        client = Bus(None, 'tok', lambda message: None, CollectingLogger())
        try:
            assert client.send(f'127.0.0.1:{port}', {'type': 'state'}, timeout=2) == {'ok': True}
        finally:
            client.stop()
    finally:
        bus.stop()


def test_the_fake_server_accept_loop_survives_a_failed_thread_start(monkeypatch):
    port = free_port()
    logger = CollectingLogger()
    fake = FakeServer('127.0.0.1', port, _fake_status, lambda: 'asleep',
                      lambda player, peer: None, logger, read_timeout=2.0, linger_timeout=0.5)
    assert fake.start()
    try:
        monkeypatch.setattr(threading, 'Thread', _BrokenThread)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                pass
            assert wait_until(lambda: fake.active_connections == 0, 3), '失败后名额没有回滚'
        finally:
            monkeypatch.undo()
        assert fake._thread.is_alive(), 'Thread.start 失败把接受线程终结了'

        result = status_ping('127.0.0.1', port, 2.0)
        assert result.online and result.is_fake, result
    finally:
        fake.stop()


# --------------------------------------------------------------------------- #
# IG-B-05 假服务端等对端关闭的总预算
# --------------------------------------------------------------------------- #

def _fake_status(protocol: int) -> dict:
    return {
        'version': {'name': 'asleep', 'protocol': protocol if protocol >= 0 else 0},
        'players': {'max': 0, 'online': 0, 'sample': []},
        'description': {'text': 'asleep'},
        '_idle_guard': {'server': 'x', 'state': 'sleeping', 'eta': 1},
    }


def test_a_trickle_client_is_released_after_the_linger_budget():
    """每 0.5 秒发 1 字节的客户端以前能把一条连接无限期占住（并发名额被耗光）。"""
    port = free_port()
    fake = FakeServer('127.0.0.1', port, _fake_status, lambda: 'asleep',
                      lambda player, peer: None, CollectingLogger(),
                      read_timeout=2.0, linger_timeout=1.0)
    assert fake.start()
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        handshake = (encode_varint(769) + encode_string('127.0.0.1') + encode_ushort(port)
                     + encode_varint(2))
        sock.sendall(pack_packet(0x00, handshake))
        sock.sendall(pack_packet(0x00, encode_string('Steve')))
        reader = ConnReader(sock)
        packet_id, _payload = reader.read_packet()
        assert packet_id == 0x00, '没有收到登录断开包'

        started = time.monotonic()
        while time.monotonic() - started < 3.0:
            try:
                sock.sendall(b'x')
            except OSError:
                break
            time.sleep(0.2)
            if fake.active_connections == 0:
                break
        assert fake.active_connections == 0, (
            f'细水长流的客户端把名额占住了 {time.monotonic() - started:.1f} 秒'
            f'（linger 预算 {fake._linger_timeout} 秒）'
        )
    finally:
        sock.close()
        fake.stop()
