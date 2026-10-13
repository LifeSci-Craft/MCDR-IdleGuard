"""假服务端的健壮性：接受线程不能被一次意外错误打死，连接数要有上限。

两条都是"对外的游戏端口"上的真实风险：

1. `accept()` 抛一次意外 `OSError`（句柄耗尽 `EMFILE`、`ECONNABORTED`…）时，接受线程如果直接
   `return`，socket 会**留在 LISTEN**、`running` 仍是 True，但再也没人去 accept ——
   玩家连接收不到任何回复、也触发不了唤醒，而且日志里一句提示都没有（实测复现过）。
   总线那处同样的坑此前已修，这里是同类的第二处。
2. 每条连接占一个线程（读超时 5 秒 + 等对端关闭 2 秒），不限个数就可能被打满线程与句柄，
   而句柄一满正好会触发第 1 条。所以：正常名额用满后还有一小段**溢出名额**（短预算，
   让玩家仍能收到提示、仍能触发唤醒），再超出就必须**有交代**地拒掉 ——
   算一次登录尝试 + 记一条 warning + 关连接，而不是像以前那样静默丢弃
   （玩家什么提示都看不到、也唤不醒服务端，控制台连一行日志都没有）。
"""
from __future__ import annotations

import socket as socket_module
import time

from idle_guard.fakeserver import MAX_CONNECTIONS, OVERFLOW_CONNECTIONS, FakeServer
from mc_test_utils import CollectingLogger, free_port, login_attempt, wait_until


def make_fake(**kwargs) -> tuple:
    """一个最小可用的假服务端 + 记录唤醒回调的列表。"""
    port = free_port()
    attempts: list = []
    logger = CollectingLogger()
    fake = FakeServer(
        '127.0.0.1', port,
        status_factory=lambda protocol: {'description': '已休眠',
                                         '_idle_guard': {'state': 'sleeping'}},
        kick_factory=lambda: '服务器已休眠，正在启动',
        on_login_attempt=lambda name, peer: attempts.append(name),
        logger=logger,
        read_timeout=kwargs.pop('read_timeout', 1.0),
        linger_timeout=kwargs.pop('linger_timeout', 0.5),
        **kwargs,
    )
    return fake, attempts, logger


# --------------------------------------------------------------------------- #
# ① 接受线程遇到意外错误后必须自愈

def test_the_fake_server_keeps_accepting_after_an_unexpected_error(monkeypatch):
    fake, attempts, logger = make_fake()
    real_accept = socket_module.socket.accept
    calls = {'n': 0}

    def flaky(self):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError(24, 'Too many open files')      # EMFILE：真实会发生
        return real_accept(self)

    monkeypatch.setattr(socket_module.socket, 'accept', flaky)
    try:
        assert fake.start(), '假服务端没起来'
        assert wait_until(lambda: '接受连接出错' in logger.text(), 5), logger.text()

        reply = None
        deadline = time.monotonic() + 12                  # 它 1 秒后重试，这里留足余量
        while time.monotonic() < deadline and reply is None:
            try:
                reply = login_attempt('127.0.0.1', fake.port, 'Steve', timeout=2.0)
            except Exception:                             # noqa: BLE001 - 还没恢复时连不上是预期的
                time.sleep(0.4)
        assert reply is not None, '接受线程没有恢复，玩家连接依然收不到任何回复'
        assert attempts == ['Steve'], '玩家连接没能触发唤醒'
    finally:
        fake.stop()


def test_stopping_the_fake_server_still_ends_the_accept_loop(monkeypatch):
    """自愈逻辑不能把"正常停止"也当成意外错误来重试（否则线程永不退出）。"""
    fake, _attempts, _logger = make_fake()
    try:
        assert fake.start()
        fake.stop()
        assert not fake.running
        thread = fake._thread
        assert thread is None or not thread.is_alive(), '接受线程没有随 stop() 退出'
    finally:
        fake.stop()


# --------------------------------------------------------------------------- #
# ② 连接数上限

def test_a_burst_is_bounded_by_the_normal_cap_plus_the_overflow_budget():
    """突发连接的总量不得超过「正常名额 + 溢出名额」。

    溢出名额是给真实玩家的：他们撞上满员时仍然要能收到提示、仍然要能唤醒服务端。
    """
    fake, _attempts, logger = make_fake(max_connections=3, max_connections_per_ip=64,
                                        read_timeout=3.0, linger_timeout=1.0)
    held: list = []
    try:
        assert fake.start()
        for _ in range(3 + OVERFLOW_CONNECTIONS + 4):      # 连上就不说话，占住处理线程
            try:
                held.append(socket_module.create_connection(('127.0.0.1', fake.port), timeout=2.0))
            except OSError:
                pass
        time.sleep(0.5)
        assert fake.active_connections <= 3 + OVERFLOW_CONNECTIONS, fake.active_connections
        assert '已达上限' in logger.text(), logger.text()
    finally:
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass
        fake.stop()


def test_connections_over_the_per_ip_cap_are_dropped_but_still_count_as_an_attempt():
    """单机把名额占满后，超出的连接**照样算一次登录尝试**（唤醒不能丢），但不再开线程。

    这是"静默失效"的回归测试：以前超限只 `_close_quietly` + `debug` 日志 ——
    玩家既看不到提示、也触发不了唤醒，而控制台一句提示都没有。
    """
    fake, attempts, logger = make_fake(max_connections=64, max_connections_per_ip=2,
                                       read_timeout=3.0, linger_timeout=1.0)
    held: list = []
    try:
        assert fake.start()
        for _ in range(5):
            held.append(socket_module.create_connection(('127.0.0.1', fake.port), timeout=2.0))
        assert wait_until(lambda: fake.active_connections <= 2, 5), fake.active_connections
        assert wait_until(lambda: len(attempts) >= 3, 5), attempts
        assert '已达上限' in logger.text(), logger.text()
    finally:
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass
        fake.stop()


def test_the_connection_counter_comes_back_down():
    """计数必须还回去，否则几轮之后假服务端会自己把自己锁死。"""
    fake, _attempts, _logger = make_fake(max_connections=4)
    try:
        assert fake.start()
        for _ in range(3):                                # 三次完整的"登录尝试"
            login_attempt('127.0.0.1', fake.port, 'Steve', timeout=2.0)
        assert wait_until(lambda: fake.active_connections == 0, 5), fake.active_connections
    finally:
        fake.stop()


def test_the_default_limit_is_sane():
    assert 8 <= MAX_CONNECTIONS <= 1024, '默认上限要能容纳正常玩家，又不至于放任线程被堆满'
