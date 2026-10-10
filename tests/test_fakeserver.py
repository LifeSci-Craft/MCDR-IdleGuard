"""假服务端（FakeServer）行为测试：状态查询、登录打断、传统查询。"""
from __future__ import annotations

import socket
import time

from idle_guard.fakeserver import FakeServer
from idle_guard.mcproto import status_ping
from mc_test_utils import CollectingLogger, free_port, login_attempt, reason_text


def build_fake(port: int, kick: str = '§e服务器已休眠，正在自动启动') -> tuple:
    events = []
    fake = FakeServer(
        bind_host='127.0.0.1',
        port=port,
        status_factory=lambda protocol: {
            'version': {'name': '§7已休眠', 'protocol': protocol},
            'players': {'max': 0, 'online': 0, 'sample': []},
            'description': {'text': '§c测试用休眠提示'},
            '_idle_guard': {'server': 'test', 'state': 'sleeping', 'eta': 42},
        },
        kick_factory=lambda: kick,
        on_login_attempt=lambda name, peer: events.append((name, peer)),
        logger=CollectingLogger(),
    )
    return fake, events


def test_status_query_returns_marker():
    port = free_port()
    fake, _ = build_fake(port)
    assert fake.start()
    try:
        result = status_ping('127.0.0.1', port, timeout=3.0)
        assert result.online
        assert result.is_fake
        assert result.marker['server'] == 'test'
        assert result.marker['eta'] == 42
        assert result.players_online == 0
        assert '休眠' in result.motd
    finally:
        fake.stop()


def test_login_attempt_is_kicked_with_reason():
    port = free_port()
    fake, events = build_fake(port, kick='§e服务器 §bsurvival §e已休眠，正在自动启动')
    assert fake.start()
    try:
        reason = login_attempt('127.0.0.1', port, name='Steve')
        text = reason_text(reason)
        assert 'survival' in text and '休眠' in text, text
        # 回调应当拿到玩家名（Velocity/客户端都会在 LoginStart 里带上名字）
        assert events and events[0][0] == 'Steve', events
    finally:
        fake.stop()


def test_login_without_name_still_kicks():
    port = free_port()
    fake, events = build_fake(port)
    assert fake.start()
    try:
        # 只发握手不发 LoginStart：仍然应当被踢并有回调（玩家名未知）
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.settimeout(5)
            from idle_guard.mcproto import encode_string, encode_ushort, encode_varint, pack_packet
            handshake = encode_varint(769) + encode_string('127.0.0.1') + encode_ushort(port) + encode_varint(2)
            sock.sendall(pack_packet(0x00, handshake))
        # 等服务端线程处理完这次连接
        deadline = time.time() + 3.0
        while time.time() < deadline and not events:
            time.sleep(0.05)
        assert events and events[0][0] is None
    finally:
        fake.stop()


def test_legacy_ping():
    port = free_port()
    fake, _ = build_fake(port)
    assert fake.start()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(b'\xfe\x01')
            data = sock.recv(2048)
        assert data.startswith(b'\xff')
        text = data[3:].decode('utf-16-be')
        assert '测试用休眠提示' in text
    finally:
        fake.stop()


def test_port_conflict_returns_false():
    port = free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(('127.0.0.1', port))
    holder.listen(1)
    fake, _ = build_fake(port)
    try:
        fake._bind_host = '127.0.0.1'
        assert fake.start() is False
        assert not fake.running
        assert fake.last_error
    finally:
        holder.close()
        fake.stop()
