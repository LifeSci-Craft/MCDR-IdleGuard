"""端口探测：POSIX 上残留的 TIME_WAIT 不能当成「端口还占着」。

背景：唤醒前要确认端口空出来了。判据如果写成「本机能不能 bind 上」，就会踩一个坑 ——
每次客户端正常断开都会在端口上留一条 TIME_WAIT，而 POSIX 上不带 SO_REUSEADDR 的 bind
会因此失败，于是「端口其实没人用」被误报成「服务端还没退干净」，唤醒一直等到超时。
（CI 上整批唤醒相关的测试就是这么挂的；本地 Windows 的 bind 语义宽松，看不出来。）

现在判据改成「还有没有人在监听」，bind 探测只留作排查用的诊断信息 —— 这里钉住它的行为。
"""
from __future__ import annotations

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


def test_probe_sets_reuseaddr_on_posix(monkeypatch):
    """POSIX 上必须开 SO_REUSEADDR，否则残留的 TIME_WAIT 会被当成「端口占着」。

    开了之后仍然绑不上**正在监听**的端口（见最后一个用例），所以结论依旧可靠。
    """
    port = free_port()          # 先取端口：下面的补丁会换掉全局 socket 工厂
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'posix'}))

    assert mcproto.bind_error('127.0.0.1', port) is None
    assert recorded.get('option') == socket_module.SO_REUSEADDR, recorded


def test_probe_leaves_windows_alone(monkeypatch):
    """Windows 上不能设 SO_REUSEADDR。

    那边的语义是「允许抢一个仍在监听的端口」，设了会让这个探测恒为「能绑」，
    「端口空出来了吗」就完全失去意义（与 fakeserver / bus 里的取舍一致）。
    """
    port = free_port()
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'nt'}))

    assert mcproto.bind_error('127.0.0.1', port) is None
    assert 'option' not in recorded, recorded


def test_probe_reports_a_listening_port():
    """真有人在监听时必须报告占用 —— 唤醒流程靠它拦住「服务端还没退干净」。"""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        assert mcproto.bind_error('127.0.0.1', port) is not None
    finally:
        listener.close()


def test_probe_is_not_fooled_by_a_time_wait():
    """刚被主动关闭的连接会在本机端口上留一条 TIME_WAIT —— 不能因此报「占着」。

    这正是唤醒流程的判据必须用「有没有人在监听」而不是「能不能 bind」的原因。
    """
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
    error = mcproto.bind_error('127.0.0.1', port)
    assert error is None, f'TIME_WAIT 被误报成「端口占着」：{error!r}'
