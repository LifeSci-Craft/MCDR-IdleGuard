"""P3 / OPT 批次（性能 / 资源 / 可维护性 / 协议）的回归测试。

- IG-A-09：心跳里的对端探测不能同步串行等（1.5 秒 × 对端数）；
- IG-A-10：自动休眠不能在轮询线程里同步跑（`stop_timeout` 默认 180 秒）；
- IG-A-11：端口接管的后台重试要有退避与总时长上限；
- IG-A-12：`_mark_peer_alive` 必须真的会被调用（原先是死代码）；
- IG-A-13：帮助页覆盖命令树上所有叶子命令，且没有零引用死键；
- IG-A-14：`_poll_loop` 与线程池的交互（原先零覆盖）；
- IG-B-07：对外游戏端口的第一条握手包要有小得多的长度上限；
- IG-B-08：velocity.toml 的单引号字符串 / IPv6 / `[ servers ]` + 跳过计数；
- IG-B-10：1.6 传统查询的长度字段是 UTF-16 码元数；
- IG-B-13：`_merge_unknown_keys` 有测试、语言文件没有零引用键。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import supervisor as sup_mod
from idle_guard import velocity as velocity_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.fakeserver import FakeServer
from idle_guard.mcproto import PingResult, encode_varint
from idle_guard.models import State
from idle_guard.supervisor import Supervisor
from mc_test_utils import CollectingLogger, StubMCDR, free_port, wait_until

PEER_ADDRESS = '127.0.0.1:25601'


def build(servers=None, *, fake_server: bool = False, **kwargs):
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
        'fake_server': FakeServerConfig(enabled=fake_server),
        'announce': AnnounceConfig(enabled=False),
        'servers': servers if servers is not None else [
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True),
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                        auto_sleep=False, bus=PEER_ADDRESS),
        ],
    }
    settings.update(kwargs)
    stub = StubMCDR()
    return Supervisor(stub, Config(**settings), plugin_version='test'), stub


# --------------------------------------------------------------------------- #
# IG-A-09 / IG-A-14 对端探测的节拍与线程池交互
# --------------------------------------------------------------------------- #

def test_peer_probes_run_in_parallel_not_one_by_one(monkeypatch):
    """对端数 1/2/4 时，探测耗时以前是 1.5s × 对端数（串行 send）。"""
    entries = [
        ServerEntry(name=f'Peer{index}', host='127.0.0.1', port=free_port(), role='watch',
                    auto_sleep=False, bus=f'127.0.0.1:{25610 + index}')
        for index in range(4)
    ]
    supervisor, _stub = build(servers=entries)
    try:
        def slow_send(address, message, timeout=None):
            time.sleep(1.0)                     # 每个对端都要等满
            return None

        monkeypatch.setattr(supervisor._bus, 'send', slow_send)
        started = time.monotonic()
        supervisor._poll_peers()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f'对端探测还是串行的（4 个对端用了 {elapsed:.2f} 秒）'
        assert set(supervisor.peer_health()) == {f'127.0.0.1:{25610 + index}' for index in range(4)}
    finally:
        supervisor.shutdown()


def test_the_poll_loop_probes_peers_even_when_the_heartbeat_is_off(monkeypatch):
    """state_tick ≤ 0 时 `_tick_states` 一次都不跑，对端探测必须有自己的调度来源。"""
    supervisor, _stub = build(state_tick=0.0, check_interval=9999.0)
    probes = {'count': 0}

    def counting():
        probes['count'] += 1

    monkeypatch.setattr(supervisor, '_poll_peers', counting)
    monkeypatch.setattr(supervisor, 'poll_once', lambda: None)
    monkeypatch.setattr(supervisor, '_tick_states', lambda: pytest.fail('心跳不该跑'))
    try:
        supervisor.start()
        assert wait_until(lambda: probes['count'] >= 1, 8), '心跳关掉后对端探测没有调度来源'
    finally:
        supervisor.shutdown()


def test_an_inbound_message_marks_the_sender_alive():
    """IG-A-12：`_mark_peer_alive` 以前是死代码；对端主动说话 = 它刚通过话。"""
    supervisor, _stub = build()
    try:
        assert supervisor.peer_health() == {}
        supervisor._on_bus_message({'type': 'state', 'instance': 'Creative'})
        health = supervisor.peer_health()
        assert any(info['instance'] == 'Creative' and info['ok'] for info in health.values()), health
        assert supervisor._is_instance_online('Creative') is True
    finally:
        supervisor.shutdown()


def test_outbound_messages_carry_our_instance_name():
    """新版本对端要能认出"这条消息是谁发的"才会刷新健康度。"""
    supervisor, _stub = build(announce=AnnounceConfig(enabled=True))
    try:
        sent: list = []
        supervisor._bus.broadcast_async = lambda addresses, message: sent.append(message)
        supervisor._announce('hello')
        supervisor._broadcast_peer_state_change()
        assert sent and all(message.get('instance') == 'Survival' for message in sent), sent
        assert [message.get('kind') for message in sent] == ['say', 'state']
    finally:
        supervisor.shutdown()


def test_poll_loop_keeps_running_when_one_round_explodes(monkeypatch):
    """IG-A-14：真正的 `_poll_loop` 以前零覆盖 —— 一轮抛异常不能把轮询线程带走。"""
    supervisor, stub = build(state_tick=0.0, check_interval=0.3)
    rounds = {'count': 0}

    def boom():
        rounds['count'] += 1
        raise RuntimeError('模拟一轮侦察炸了')

    monkeypatch.setattr(supervisor, 'poll_once', boom)
    try:
        supervisor.start()
        assert wait_until(lambda: rounds['count'] >= 2, 10), f'轮询线程死了：{rounds}'
        assert supervisor._poll_thread.is_alive()
        assert '轮询时出现异常' in stub.logger.text()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-10 自动休眠不在轮询线程里同步跑
# --------------------------------------------------------------------------- #

def test_auto_sleep_does_not_block_the_poll_thread(monkeypatch):
    supervisor, _stub = build()
    try:
        managed = supervisor.state_of('Survival')
        managed.state = State.RUNNING
        managed.entry.idle_timeout = 10.0
        managed.started_at = time.monotonic() - 100.0
        managed.empty_since = time.monotonic() - 100.0
        managed.slp = PingResult(online=True, players_online=0, players_max=20)
        started = threading.Event()
        release = threading.Event()

        def slow_sleep(name, reason='manual', by='system'):
            started.set()
            release.wait(5)
            return True, 'ok'

        monkeypatch.setattr(supervisor, 'sleep_server', slow_sleep)
        began = time.monotonic()
        supervisor._check_idle(managed)
        elapsed = time.monotonic() - began
        try:
            assert elapsed < 0.5, f'自动休眠阻塞了轮询线程 {elapsed:.2f} 秒'
            assert started.wait(5), '自动休眠没有被转后台'
        finally:
            release.set()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-11 端口接管的后台重试：退避 + 总时长上限
# --------------------------------------------------------------------------- #

def test_the_takeover_retry_loop_gives_up_and_logs(monkeypatch):
    supervisor, stub = build(fake_server=True)
    try:
        managed = supervisor.state_of('Survival')
        managed.state = State.OFFLINE
        attempts = {'count': 0}

        def never_binds(m, force=False, retry=False):
            attempts['count'] += 1

        monkeypatch.setattr(supervisor, '_ensure_fake_server', never_binds)
        monkeypatch.setattr(supervisor, '_can_force_take_over', lambda m: False)
        monkeypatch.setattr(sup_mod, '_TAKEOVER_RETRY_MAX_SECONDS', 1.0)
        started = time.monotonic()
        supervisor._takeover_retry_loop(managed)
        elapsed = time.monotonic() - started
        assert elapsed < 6.0, '放弃得太晚（退避上限没生效）'
        assert attempts['count'] >= 2, '退避把重试次数压到 1 次以下了'
        assert '放弃后台重试' in stub.logger.text(), stub.logger.text()
        assert managed.detail and '放弃' in managed.detail
    finally:
        supervisor.shutdown()


def test_the_fake_server_refuses_to_bind_while_the_real_server_is_alive(monkeypatch):
    """反向守卫：真实服务端还活着时绝不抢端口（抢走玩家连接比"暂时连不上"严重得多）。"""
    supervisor, stub = build(fake_server=True)
    try:
        managed = supervisor.state_of('Survival')
        stub.running = True
        assert supervisor._can_start_fake(managed) is False, 'MCDR 说服务端在跑，还是要抢端口'
        managed.state = State.OFFLINE
        supervisor._ensure_fake_server(managed, retry=False)
        assert managed.fake is None or not managed.fake.running

        # role=command：端口上有人在应答时同样不抢
        entries = [ServerEntry(name='Cmd', host='127.0.0.1', port=free_port(), role='command',
                               start_command='true', auto_sleep=False)]
        other, _stub2 = build(servers=entries, fake_server=True)
        try:
            monkeypatch.setattr(other, '_probe_port', lambda *a, **k: True)
            assert other._can_start_fake(other.state_of('Cmd')) is False
        finally:
            other.shutdown()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-A-13 / IG-B-13 帮助页覆盖 + 语言文件没有零引用键
# --------------------------------------------------------------------------- #

def test_the_help_page_covers_every_leaf_command():
    """命令树上的每个叶子命令都要出现在 `_HELP_ROWS` 里，且三件套文案都存在。"""
    import re

    from idle_guard import i18n

    supervisor, _stub = build()
    try:
        root = cmd_mod.build_tree(supervisor, '!!ig')
        leaves = set()

        def literal_name(node):
            match = re.match(r"Literal '([^']+)'", str(node))
            return match.group(1) if match else None

        def walk(node, parts):
            kids = [kid for kid in node.get_children() if literal_name(kid)]
            if not kids:
                leaves.add(parts[-1] if parts else '')
                return
            for kid in kids:
                walk(kid, parts + [literal_name(kid)])

        walk(root, [])
        assert {'unlock', 'cancel', 'lock', 'confirm'} <= leaves, leaves
        keys = {key for key, _bare in cmd_mod._HELP_ROWS}
        missing = {leaf for leaf in leaves if leaf != 'help' and leaf not in keys}
        assert missing == set(), f'帮助页缺这些命令：{missing}'
        for key in keys:
            for prefix in ('command.help.cmd.', 'command.help.desc.', 'command.help.hover.'):
                assert i18n.has(prefix + key), f'语言文件缺少 {prefix}{key}'
    finally:
        supervisor.shutdown()


#: 通过 f-string 拼出来的键前缀（无法用"字面量是否出现"判断，必须白名单）
_DYNAMIC_KEY_PREFIXES = ('command.help.cmd.', 'command.help.desc.', 'command.help.hover.',
                         'command.status.', 'state.', 'role.',
                         'hover.state.', 'hover.role.', 'hover.switch.')


def test_no_language_key_is_unreferenced():
    """两个死键（`detail.locked_after_failures` / `detail.takeover_retrying`）留下的教训。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package = os.path.join(here, 'idle_guard')
    sources = []
    for name in os.listdir(package):
        if name.endswith('.py'):
            with open(os.path.join(package, name), encoding='utf-8') as handle:
                sources.append(handle.read())
    sources.append(open(os.path.abspath(cmd_mod.__file__), encoding='utf-8').read())
    blob = '\n'.join(sources)

    dead = []
    for language in ('zh_cn', 'en_us'):
        with open(os.path.join(package, 'lang', f'{language}.json'), encoding='utf-8') as handle:
            catalog = json.load(handle)
        for key in catalog:
            if key.startswith(_DYNAMIC_KEY_PREFIXES):
                continue
            if f"'{key}'" not in blob and f'"{key}"' not in blob:
                dead.append(f'{language}:{key}')
    assert dead == [], f'这些语言键在代码里零引用：{dead}'


def test_merge_unknown_keys_keeps_user_additions():
    """第一轮修复（`_merge_unknown_keys`）以前没有任何测试。"""
    old = {'my_note': 'keep me', 'bus': {'token': 'mine', 'extra': 1}, 'sleep': {'mine': True}}
    new = {'bus': {'token': 'new', 'bind': 'auto'}, 'sleep': {}, 'debug': False}
    merged = sup_mod._merge_unknown_keys(old, new)
    assert merged['my_note'] == 'keep me', '未知顶层键被丢掉了'
    assert merged['bus']['extra'] == 1, '组内未知键被丢掉了'
    assert merged['bus']['token'] == 'new', '已知键必须用新值'
    assert merged['sleep'] == {'mine': True}
    assert merged['debug'] is False


# --------------------------------------------------------------------------- #
# IG-B-07 假服务端第一条握手包的长度上限
# --------------------------------------------------------------------------- #

def test_read_packet_honours_the_max_length():
    """只信长度前缀就等于让任何能连上端口的人要求我们准备 2 MiB 缓冲。"""
    from idle_guard.mcproto import ConnReader

    server, client = socket.socketpair()
    try:
        client.sendall(encode_varint(100000) + b'\x00' * 16)
        with pytest.raises(ValueError):
            ConnReader(server).read_packet(8192)
    finally:
        server.close()
        client.close()


def test_a_huge_first_packet_is_rejected_by_the_fake_server():
    """对外游戏端口未鉴权：超长首包必须**立刻**断开，而不是等满读超时。"""
    port = free_port()
    fake = FakeServer('127.0.0.1', port, lambda protocol: {'players': {}}, lambda: 'x',
                      lambda player, peer: None, CollectingLogger(), read_timeout=2.0,
                      linger_timeout=0.2)
    assert fake.start()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
            # 声明一个 2 MiB 的包，但只发几个字节
            sock.sendall(encode_varint(2 ** 21 - 1) + b'\x00\x01\x02')
            sock.settimeout(5.0)
            started = time.monotonic()
            assert sock.recv(16) == b'', '超长首包没有被直接断开'
            elapsed = time.monotonic() - started
        assert elapsed < 1.0, f'没有立刻断开（等了 {elapsed:.2f} 秒，说明在读满 2 MiB）'
    finally:
        fake.stop()
    assert fake.active_connections == 0


def test_the_handshake_packet_cap_is_much_smaller_than_the_protocol_limit():
    from idle_guard import fakeserver as fakeserver_mod
    from idle_guard import mcproto as mcproto_mod

    assert fakeserver_mod.MAX_CLIENT_PACKET_LENGTH < mcproto_mod.MAX_PACKET_LENGTH // 100


# --------------------------------------------------------------------------- #
# IG-B-08 velocity.toml 的写法兼容与跳过计数
# --------------------------------------------------------------------------- #

VELOCITY_SAMPLE = '''\
[ servers ]
Survival = "127.0.0.1:30000"
Creative = '127.0.0.1:30001'
IPv6 = "[::1]:30004"
try = ["Survival"]
# a comment
Broken = "no-port-here"
Numeric = 30005
Name = "with # inside"
'''


def test_velocity_accepts_single_quotes_ipv6_and_spaced_section_header(tmp_path):
    path = tmp_path / 'velocity.toml'
    path.write_text(VELOCITY_SAMPLE, encoding='utf-8')
    entries = velocity_mod.parse_servers(str(path))
    assert entries['Survival'] == ('127.0.0.1', 30000)
    assert entries['Creative'] == ('127.0.0.1', 30001), '单引号（TOML 字面字符串）被丢掉了'
    assert entries['IPv6'] == ('::1', 30004), 'IPv6 后端地址被丢掉了'
    assert 'try' not in entries and 'Numeric' not in entries


def test_velocity_reports_the_lines_it_skipped(tmp_path):
    path = tmp_path / 'velocity.toml'
    path.write_text(VELOCITY_SAMPLE, encoding='utf-8')
    entries, skipped = velocity_mod.parse_servers_detailed(str(path))
    assert len(entries) == 3
    assert any('no-port-here' in line for line in skipped), skipped
    assert any('Numeric' in line for line in skipped), skipped


def test_velocity_load_servers_still_works(tmp_path):
    path = tmp_path / 'velocity.toml'
    path.write_text(VELOCITY_SAMPLE, encoding='utf-8')
    found_path, entries = velocity_mod.load_servers(str(path), str(tmp_path))
    assert found_path == str(path)
    assert set(entries) == {'Survival', 'Creative', 'IPv6'}
    found_path, entries, skipped = velocity_mod.load_servers_detailed(str(path), str(tmp_path))
    assert skipped


def test_check_reports_skipped_velocity_lines(tmp_path):
    from idle_guard import diag as diag_mod
    from idle_guard.config import VelocityConfig

    path = tmp_path / 'velocity.toml'
    path.write_text(VELOCITY_SAMPLE, encoding='utf-8')
    supervisor, _stub = build(velocity=VelocityConfig(enabled=True, path=str(path)))
    try:
        status = supervisor.velocity_status()
        assert status['state'] == 'ok', status
        assert status['skipped'] >= 1, status
        rows = '\n'.join(line for line, _colour in diag_mod.check_rows(supervisor))
        assert '跳过' in rows, rows
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# IG-B-10 1.6 传统查询的长度字段
# --------------------------------------------------------------------------- #

def test_legacy_query_declares_the_utf16_unit_count():
    port = free_port()
    status = {
        'version': {'name': 'v', 'protocol': 4},
        'players': {'online': 0, 'max': 0},
        'description': {'text': '😀 asleep'},
    }
    fake = FakeServer('127.0.0.1', port, lambda protocol: status, lambda: 'x',
                      lambda player, peer: None, CollectingLogger(),
                      read_timeout=2.0, linger_timeout=0.2)
    assert fake.start()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
            sock.sendall(b'\xfe')
            sock.settimeout(3.0)
            data = b''
            declared = None
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if declared is None and len(data) >= 3:
                    declared = int.from_bytes(data[1:3], 'big')
                if declared is not None and len(data) >= 3 + declared * 2:
                    break
        assert declared is not None, f'没读到传统查询应答：{data!r}'
        body = data[3:].decode('utf-16-be', errors='replace')
        units = len(body.encode('utf-16-le')) // 2       # 协议里的长度 = UTF-16 码元数
        assert declared == units, (
            f'声明 {declared} 个码元，实际 {units} 个（非 BMP 字符在 UTF-16 里占 2 个）'
        )
        assert '😀' in body
    finally:
        fake.stop()
