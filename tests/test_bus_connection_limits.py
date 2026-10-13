"""总线的地址校验与连接治理（代码审查后的加固）。

三条都来自审查：

1. **端口必须做范围校验**：`parse_address` 以前只查 `isdigit()`，于是 `"127.0.0.1:99999"`
   会被当成合法地址，一路走到 `socket.bind` / `create_connection` 才抛 `OverflowError` ——
   那不是 `OSError`，`Bus.start()` 接不住、`on_load` 也接不住，配置里一个笔误
   （`bus.bind` 少写一位）就能让**整个插件加载失败**。
2. **连接要名额**：每条连接占一个线程，而且在「握手超时」之前一直占着；以前一个上限都没有，
   鉴权之前还允许读入 1 MiB。现在：总连接数与每 IP 连接数都有闸门，hello 之前只读 4 KiB。
3. **口令比较改成常量时间**（顺带兼容非 ASCII 口令，避免 `compare_digest` 抛 TypeError）。
"""
from __future__ import annotations

import socket

import pytest

from idle_guard.bus import (
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_IP,
    Bus,
    parse_address,
)
from mc_test_utils import CollectingLogger, free_port, wait_until


def make_bus(bind_address=None, token: str = 's3cret', timeout: float = 3.0) -> tuple:
    logger = CollectingLogger()
    bus = Bus(bind_address=bind_address, token=token, handler=lambda message: {'ok': True},
              logger=logger, timeout=timeout)
    return bus, logger


# --------------------------------------------------------------------------- #
# ① 地址校验

@pytest.mark.parametrize('address', ['127.0.0.1:99999', '127.0.0.1:65536', '127.0.0.1:0',
                                     '127.0.0.1:', 'nonsense', '', None, ':25600', '127.0.0.1:abc'])
def test_invalid_addresses_are_rejected_up_front(address):
    assert parse_address(address) is None


@pytest.mark.parametrize('address,expected', [
    ('127.0.0.1:25600', ('127.0.0.1', 25600)),
    ('[::1]:25601', ('::1', 25601)),
    (' 127.0.0.1:25602 ', ('127.0.0.1', 25602)),
    ('127.0.0.1:65535', ('127.0.0.1', 65535)),
])
def test_valid_addresses_still_parse(address, expected):
    assert parse_address(address) == expected


def test_an_out_of_range_port_is_reported_instead_of_raising():
    """坏端口要变成「总线没起来 + 一条日志」，绝不能抛出 OverflowError 穿透 on_load。"""
    bus, logger = make_bus(bind_address='127.0.0.1:99999')
    try:
        assert bus.start() is False
        assert '非法' in logger.text(), logger.text()
    finally:
        bus.stop()


def test_send_to_an_out_of_range_port_fails_softly():
    bus, _logger = make_bus()
    try:
        assert bus.send('127.0.0.1:99999', {'type': 'state'}) is None
    finally:
        bus.stop()


# --------------------------------------------------------------------------- #
# ② 口令比较

def test_token_comparison_accepts_only_the_exact_token():
    bus, _logger = make_bus(token='s3cret')
    try:
        assert bus._token_matches('s3cret') is True
        assert bus._token_matches('s3cres') is False
        assert bus._token_matches('') is False
        assert bus._token_matches('S3CRET') is False
        assert bus._token_matches('口令') is False      # 非 ASCII：不能抛异常
    finally:
        bus.stop()


# --------------------------------------------------------------------------- #
# ③ 连接名额

def test_acquire_enforces_the_per_ip_cap_then_the_global_cap():
    bus, _logger = make_bus()
    try:
        assert all(bus._acquire('10.0.0.1') for _ in range(MAX_CONNECTIONS_PER_IP))
        assert bus._acquire('10.0.0.1') is False, '同一个 IP 不该能超过每 IP 上限'

        index = 2
        while bus._acquire(f'10.0.0.{index}'):
            index += 1
        assert bus._active == MAX_CONNECTIONS, bus._active
        assert bus._acquire('10.9.9.9') is False, '全局上限之后不该再放进新的 IP'

        bus._release('10.0.0.1')
        assert bus._active == MAX_CONNECTIONS - 1
        assert bus._acquire('10.9.9.9') is True, '释放出来的名额应当能被重新占用'
    finally:
        bus.stop()


def test_connections_over_the_per_ip_cap_are_closed_and_logged():
    """超出每 IP 上限的连接：不开线程、直接关掉，而且**必须留日志**。

    以前总线连一个上限都没有，表现成对端"莫名其妙连不上"而本端一声不响。
    """
    port = free_port()
    bus, logger = make_bus(bind_address=f'127.0.0.1:{port}', timeout=3.0)
    held: list = []
    try:
        assert bus.start()
        for _ in range(MAX_CONNECTIONS_PER_IP + 3):
            try:
                held.append(socket.create_connection(('127.0.0.1', port), timeout=2.0))
            except OSError:
                pass
        assert wait_until(lambda: '已达上限' in logger.text(), 5), logger.text()
        assert bus._active <= MAX_CONNECTIONS_PER_IP, bus._active
    finally:
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass
        bus.stop()


def test_the_token_is_not_compared_with_a_plain_equality(monkeypatch):
    """回归：口令校验必须走 hmac.compare_digest，而不是 `==`。"""
    import idle_guard.bus as bus_module

    seen: list = []
    real = bus_module.hmac.compare_digest

    def spy(left, right):
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(bus_module.hmac, 'compare_digest', spy)
    bus, _logger = make_bus(token='abc')
    try:
        assert bus._token_matches('abc') is True
        assert seen, '没有走常量时间比较'
    finally:
        bus.stop()
