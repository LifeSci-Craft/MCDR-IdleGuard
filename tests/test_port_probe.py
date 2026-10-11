"""端口探测：判断「端口空出来了吗」不能问「我能不能 bind 上」。

背景：唤醒前要确认端口空出来了。判据如果写成「本机能不能 bind 上」，就会踩一个坑 ——
每次客户端正常断开都会在端口上留一条 TIME_WAIT，而 bind 在 TIME_WAIT 面前会直接报
EADDRINUSE（实测 POSIX 上连 SO_REUSEADDR 都救不了），于是「端口其实没人用」被误报成
「服务端还没退干净」，唤醒一路等到超时。CI 上整批唤醒相关的测试就是这么挂的；
本地 Windows 因为 bind 语义宽松，看不到。

现在判据是「还有没有人在监听」（`port_open`），bind 探测只留作排查用的诊断信息。
"""
from __future__ import annotations

import os
import socket as socket_module
import time

from idle_guard import mcproto
from mc_test_utils import free_port


class _RecordingSocket:
    """socket 的薄代理：记下 setsockopt 的参数，其余原样转发。

    （socket 是 C 实现的只读类型，没法直接给实例打补丁。）
    """

    def __init__(self, sock, recorded: dict) -> None:
        self._sock = sock
        self._recorded = recorded

    def setsockopt(self, level, option, value):
        self._recorded['option'] = option
        return self._sock.setsockopt(level, option, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._sock.close()
        return False

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _spy_socket(monkeypatch, recorded: dict) -> None:
    """把 mcproto 用到的 socket 工厂换成「记录 setsockopt 之后照常工作」的替身。"""
    real_socket = socket_module.socket

    def spy(*args, **kwargs):
        return _RecordingSocket(real_socket(*args, **kwargs), recorded)

    monkeypatch.setattr(mcproto.socket, 'socket', spy)


def _closed_connection_on_its_own_port() -> int:
    """造一个「刚被本机主动关闭、还挂着 TIME_WAIT」的端口，返回端口号。"""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    client = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    client.connect(('127.0.0.1', port))
    accepted, _peer = listener.accept()

    accepted.close()            # 本机侧主动关闭 → 这个端口上留下 TIME_WAIT
    try:
        client.recv(1)          # 收下对端的 FIN，推进会话收尾
    except OSError:
        pass
    client.close()
    listener.close()
    time.sleep(0.2)             # 给内核一点时间把连接推进到 TIME_WAIT
    return port


# --------------------------------------------------------------------------- #
# ① 正式判据：还有没有人在监听
# --------------------------------------------------------------------------- #

def test_port_open_reports_a_listening_port():
    """有人在监听时必须为 True —— 唤醒流程靠它拦住「服务端还没退干净」。"""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        assert mcproto.port_open('127.0.0.1', port, 0.5) is True
    finally:
        listener.close()


def test_port_open_is_not_fooled_by_a_time_wait():
    """刚关闭的连接留下的 TIME_WAIT 不能让判据以为「还有人在监听」。

    这正是判据选 `port_open` 而不是 bind 探测的理由：同一时刻 bind 会报 EADDRINUSE
    （见下面最后一个用例），而唤醒真正关心的只有「还有没有人在监听」。
    """
    port = _closed_connection_on_its_own_port()
    assert mcproto.port_open('127.0.0.1', port, 0.5) is False


def test_port_open_is_false_on_a_port_nobody_uses():
    assert mcproto.port_open('127.0.0.1', free_port(), 0.5) is False


# --------------------------------------------------------------------------- #
# ② 诊断用的 bind 探测（不再参与判断，只帮排查）
# --------------------------------------------------------------------------- #

def test_probe_sets_reuseaddr_on_posix(monkeypatch):
    """POSIX 上设 SO_REUSEADDR，与 fakeserver / bus 的取舍保持一致。"""
    port = free_port()          # 先取端口：下面的补丁会换掉全局 socket 工厂
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'posix'}))

    assert mcproto.bind_error('127.0.0.1', port) is None
    assert recorded.get('option') == socket_module.SO_REUSEADDR, recorded


def test_probe_leaves_windows_alone(monkeypatch):
    """Windows 上不能设 SO_REUSEADDR。

    那边的语义是「允许抢一个仍在监听的端口」，设了会让这个探测恒为「能绑」。
    """
    port = free_port()
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'nt'}))

    assert mcproto.bind_error('127.0.0.1', port) is None
    assert 'option' not in recorded, recorded


def test_probe_reports_a_listening_port():
    """真有人在监听时，bind 探测也要报告占用（它是错误日志里的佐证）。"""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        assert mcproto.bind_error('127.0.0.1', port) is not None
    finally:
        listener.close()


def test_probe_behaviour_around_a_time_wait():
    """钉住「bind 探测会被 TIME_WAIT 骗到」，免得以后又有人拿它当判据。

    POSIX 上直接报 EADDRINUSE —— SO_REUSEADDR 也救不了（那个标志管的是「与另一个
    socket 共用地址」的协商，不解除 TIME_WAIT 的占用）。Windows 的 bind 不理会
    TIME_WAIT，那边照样能绑上 —— **这个差异正是本地跑不出 CI 失败的原因**。
    """
    port = _closed_connection_on_its_own_port()
    error = mcproto.bind_error('127.0.0.1', port)
    if os.name == 'nt':
        assert error is None, f'Windows 的 bind 不理会 TIME_WAIT，却报了 {error!r}'
    else:
        assert error is not None, 'POSIX 上预期 bind 会被 TIME_WAIT 挡住，结果却成功了'
