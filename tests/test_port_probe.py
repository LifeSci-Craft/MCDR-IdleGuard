"""端口探测的平台差异：POSIX 上判断「端口是否已释放」必须绕开 TIME_WAIT。

背景：唤醒前要先确认端口空出来了（`can_bind`）。但在 POSIX 上，刚撤下假服务端
的那段时间里端口上还挂着 TIME_WAIT（此前每一次状态查询、每一次玩家握手留下的），
不带 SO_REUSEADDR 的 bind 会失败 —— 于是被误判成「服务端还没退干净」，唤醒一直
等到超时。Windows 的 bind 语义宽松，所以本地跑不出来，CI（Linux）上整批唤醒相关
的测试挂了才暴露。
"""
from __future__ import annotations

import socket as socket_module

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
    """把 mcproto 用到的 socket 工厂换成"记录 setsockopt 之后照常工作"的替身。"""
    real_socket = socket_module.socket

    def spy(*args, **kwargs):
        return _RecordingSocket(real_socket(*args, **kwargs), recorded)

    monkeypatch.setattr(mcproto.socket, 'socket', spy)


def test_can_bind_enables_reuseaddr_on_posix(monkeypatch):
    """POSIX 上必须开 SO_REUSEADDR，否则 TIME_WAIT 会被当成「端口还被占着」。

    开了之后仍然绑不上**正在监听**的端口（见下面第三个用例），所以判断依旧可靠。
    """
    port = free_port()          # 先取端口：下面的补丁会换掉全局 socket 工厂
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'posix'}))

    assert mcproto.can_bind('127.0.0.1', port) is True
    assert recorded.get('option') == socket_module.SO_REUSEADDR, recorded


def test_can_bind_leaves_windows_alone(monkeypatch):
    """Windows 上不能设 SO_REUSEADDR。

    那边的语义是「允许抢一个仍在监听的端口」，设了会让这个探测恒为 True，
    「端口空出来了吗」就完全失去意义了（与 fakeserver / bus 里的取舍一致）。
    """
    port = free_port()          # 先取端口：下面的补丁会换掉全局 socket 工厂
    recorded: dict = {}
    _spy_socket(monkeypatch, recorded)
    monkeypatch.setattr(mcproto, 'os', type('FakeOS', (), {'name': 'nt'}))

    assert mcproto.can_bind('127.0.0.1', port) is True
    assert 'option' not in recorded, recorded


def test_can_bind_reports_a_listening_port_as_busy():
    """真有人在监听时必须返回 False —— 这是唤醒前的反向守卫，两个平台都要成立。"""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        assert mcproto.can_bind('127.0.0.1', port) is False
    finally:
        listener.close()
