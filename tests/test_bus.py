"""实例间总线（Bus）测试。"""
from __future__ import annotations

from idle_guard.bus import Bus, parse_address
from mc_test_utils import CollectingLogger, free_port


def test_parse_address():
    assert parse_address('127.0.0.1:25600') == ('127.0.0.1', 25600)
    assert parse_address('example.com:1') == ('example.com', 1)
    assert parse_address('[::1]:25600') == ('::1', 25600)
    assert parse_address('') is None
    assert parse_address('127.0.0.1') is None
    assert parse_address('127.0.0.1:abc') is None


def test_request_reply_and_token_check():
    port = free_port()
    received = []

    def handler(message: dict):
        received.append(message)
        if message.get('type') == 'state':
            return {'ok': True, 'instance': 'lobby', 'servers': [{'name': 'lobby', 'state': 'running'}]}
        if message.get('type') == 'announce':
            return {'ok': True, 'delivered': message.get('player') == 'Steve'}
        return {'ok': False, 'reason': 'unknown'}

    server = Bus(f'127.0.0.1:{port}', 'secret', handler, CollectingLogger())
    assert server.start()
    client = Bus(None, 'secret', lambda m: None, CollectingLogger())
    try:
        reply = client.send(f'127.0.0.1:{port}', {'type': 'state'})
        assert reply and reply['ok'] and reply['instance'] == 'lobby'

        reply = client.send(f'127.0.0.1:{port}', {'type': 'announce', 'kind': 'tell', 'player': 'Steve'})
        assert reply and reply['delivered'] is True

        # token 不匹配时必须被拒绝
        intruder = Bus(None, 'wrong-token', lambda m: None, CollectingLogger())
        assert intruder.send(f'127.0.0.1:{port}', {'type': 'state'}) is None

        # 端口没人监听时应当安静地失败
        assert client.send(f'127.0.0.1:{free_port()}', {'type': 'state'}, timeout=0.5) is None

        assert [m['type'] for m in received] == ['state', 'announce']
    finally:
        server.stop()
        client.stop()


def test_broadcast():
    ports = [free_port() for _ in range(2)]
    buses = []
    try:
        for index, port in enumerate(ports):
            bus = Bus(f'127.0.0.1:{port}', 'tok',
                      lambda m, i=index: {'ok': True, 'instance': f'server{i}'}, CollectingLogger())
            assert bus.start()
            buses.append(bus)
        client = Bus(None, 'tok', lambda m: None, CollectingLogger())
        replies = client.broadcast([f'127.0.0.1:{p}' for p in ports], {'type': 'state'}, timeout=2.0)
        assert sorted(r['instance'] for r in replies) == ['server0', 'server1']
    finally:
        for bus in buses:
            bus.stop()
