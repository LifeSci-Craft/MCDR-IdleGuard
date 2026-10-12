"""
假服务端（Fake Server）：在真实服务端休眠期间顶替它的端口

职责：
1. 响应状态查询（SLP）：返回可自定义的 MOTD / 版本名 / 0 人在线，并附带 `_idle_guard` 标记，
   便于本插件自身区分"真服务端"与"假服务端"；
2. 处理登录请求：尽力解析尝试连接的玩家名 -> 回调 on_login_attempt -> 立即以自定义文本断开连接。
   通过 Velocity 代理连接时，这个断开原因会被代理转发给玩家（玩家还留在原来的子服里）；
   直连时则显示在客户端的断开界面上；
3. 兼容 1.6 及更早的 0xFE 传统查询。

设计取舍：
- 端口被占用时 start() 直接返回 False，不抛异常 —— "端口还被真实服务端占着" 是正常情况，
  由上层（supervisor）在轮询里重试；
- 主动关闭连接的一侧会留下 TIME_WAIT，因此这里在发完断开包后会等待对端先关（最多 linger_timeout 秒），
  尽量避免本机侧堆积 TIME_WAIT 而影响后续端口绑定；
- 刻意不在 Windows 上使用 SO_REUSEADDR（Windows 下它允许抢占仍在监听的端口，会掩盖真实状态），
  在 POSIX 上则使用它来绕过 TIME_WAIT 造成的绑定失败。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Callable, Optional

from .mcproto import (
    ConnReader,
    HANDSHAKE_NEXT_STATE_LOGIN,
    HANDSHAKE_NEXT_STATE_STATUS,
    component_to_text,
    decode_varint,
    encode_string,
    encode_ushort,
    pack_packet,
    parse_string,
)


#: 同时处理的连接数上限。这是对外的游戏端口，所以给个硬上限：
#: 每条连接占一个线程（读超时 5 秒 + 等对端关闭 2 秒），不限个数就有可能被打满线程与句柄；
#: 句柄一满还会连带把接受循环打挂（EMFILE）。正常玩家与实例间探测远到不了这个数，
#: 所以它只在被扫时才起作用。不做成配置项：它没有需要按机器区分的场景。
MAX_CONNECTIONS = 64


class FakeServer:
    def __init__(
        self,
        bind_host: str,
        port: int,
        status_factory: Callable[[int], dict],
        kick_factory: Callable[[], str],
        on_login_attempt: Callable[[Optional[str], str], None],
        logger,
        read_timeout: float = 5.0,
        linger_timeout: float = 2.0,
        max_connections: int = MAX_CONNECTIONS,
    ):
        self._bind_host = bind_host
        self._port = int(port)
        self._status_factory = status_factory
        self._kick_factory = kick_factory
        self._on_login_attempt = on_login_attempt
        self._logger = logger
        self._read_timeout = read_timeout
        self._linger_timeout = linger_timeout
        self._max_connections = max(1, int(max_connections))
        #: 当前正在处理的连接数（受 _max_connections 限制）
        self._active = 0

        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_error = ''
        self.login_attempt_count = 0

    # ------------------------------------------------------------------ 生命周期

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def active_connections(self) -> int:
        """当前正在处理的连接数（诊断用；上限见 MAX_CONNECTIONS）。"""
        with self._lock:
            return self._active

    def start(self, force: bool = False) -> bool:
        """尝试绑定端口并开始监听。端口不可用（说明真实服务端还在跑）时返回 False。

        force=True 时用 SO_REUSEADDR 强行接管：这能在 Windows 上绕过"端口还残留着
        TIME_WAIT/CLOSE_WAIT 连接就拒绝 bind"的限制，代价是**也会抢占一个仍在监听的端口**。
        所以只在调用方已确认真实服务端不在跑时才可以用（见 Supervisor._can_force_take_over）。
        """
        with self._lock:
            if self._running:
                return True
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if force or os.name != 'nt':
                # POSIX：允许在 TIME_WAIT 残留时重新绑定（不会抢占正在监听的端口）
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self._bind_host, self._port))
                sock.listen(32)
            except OSError as exc:
                self._last_error = f'{type(exc).__name__}: {exc}'
                sock.close()
                return False
            self._sock = sock
            self._running = True
            self._last_error = ''
            self._thread = threading.Thread(
                target=self._accept_loop,
                name=f'IdleGuard-FakeServer-{self._port}',
                daemon=True,
            )
            self._thread.start()
            self._logger.info(f'假服务端已在 {self._bind_host}:{self._port} 上监听'
                              + ('（强制接管）' if force else ''))
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._logger.info(f'假服务端已从 {self._bind_host}:{self._port} 上撤下')

    # ------------------------------------------------------------------ 内部实现

    def _accept_loop(self) -> None:
        while True:
            with self._lock:
                sock = self._sock
                if not self._running or sock is None:
                    return
            try:
                conn, addr = sock.accept()
            except OSError as exc:
                if not self.running:
                    return              # stop() 关掉了监听的 socket：正常退出
                # ⚠️ 意外错误**不能**让接受线程退出：socket 还留在 LISTEN、`running` 仍是 True，
                # 但再也没人去 accept —— 玩家连接收不到任何回复、也触发不了唤醒，而日志里
                # 一句提示都没有（实测复现过：一次 EMFILE 之后就永久失效）。总线那处同样的坑
                # 此前已修（见 bus._accept_loop 的注释）。这里记一条日志、歇一下继续转。
                self._last_error = f'{type(exc).__name__}: {exc}'
                self._logger.warning(f'假服务端接受连接出错（{self._last_error}），1 秒后继续')
                time.sleep(1.0)
                continue

            with self._lock:
                room = self._active < self._max_connections
                if room:
                    self._active += 1
            if not room:
                # 并发上限：这是**对外的游戏端口**，不限个数的话几百个"连上不发数据"的连接
                # 就能把线程与句柄堆满；句柄一满还会把上面的 accept 打挂（EMFILE）。
                self._logger.debug(f'假服务端并发连接已达上限 {self._max_connections}，'
                                   f'丢弃来自 {addr[0]} 的连接')
                self._close_quietly(conn)
                continue

            threading.Thread(
                target=self._serve_connection,
                args=(conn, f'{addr[0]}:{addr[1]}'),
                name=f'IdleGuard-FakeConn-{self._port}',
                daemon=True,
            ).start()

    def _serve_connection(self, conn: socket.socket, peer: str) -> None:
        """跑完一条连接，并把并发计数还回去（计数在 `_accept_loop` 里加）。"""
        try:
            self._handle(conn, peer)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)

    @staticmethod
    def _close_quietly(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    def _handle(self, conn: socket.socket, peer: str) -> None:
        try:
            conn.settimeout(self._read_timeout)
            reader = ConnReader(conn)
            if reader.peek(1)[:1] == b'\xfe':
                self._handle_legacy(conn)
                return

            packet_id, payload = reader.read_packet()
            if packet_id != 0x00:
                return
            offset = 0
            protocol, offset = decode_varint(payload, offset)
            _server_address, offset = parse_string(payload, offset)
            offset += 2  # server port (unsigned short)
            next_state, offset = decode_varint(payload, offset)

            if next_state == HANDSHAKE_NEXT_STATE_STATUS:
                self._handle_status(conn, reader, protocol)
            elif next_state == HANDSHAKE_NEXT_STATE_LOGIN:
                self._handle_login(conn, reader, peer)
        except (OSError, ValueError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001 - 假服务端不能因为单个连接异常而崩溃
            self._logger.debug(f'假服务端处理 {peer} 的连接时出错: {type(exc).__name__}: {exc}')
        finally:
            self._close_quietly(conn)

    def _handle_status(self, conn: socket.socket, reader: ConnReader, protocol: int = -1) -> None:
        packet_id, _ = reader.read_packet()
        if packet_id != 0x00:  # Status Request
            return
        status = self._status_factory(protocol)
        conn.sendall(pack_packet(0x00, encode_string(json.dumps(status, ensure_ascii=False))))
        # 可选：回一个 Pong，让客户端的延迟测量正常完成
        try:
            packet_id, payload = reader.read_packet()
            if packet_id == 0x01 and len(payload) >= 8:
                conn.sendall(pack_packet(0x01, payload[:8]))
        except (OSError, ValueError, ConnectionError):
            pass

    def _handle_login(self, conn: socket.socket, reader: ConnReader, peer: str) -> None:
        player_name: Optional[str] = None
        try:
            packet_id, payload = reader.read_packet()
            if packet_id == 0x00:  # Login Start
                # 字段顺序为 name [+ 后续字段]，只需读取第一个 String 即可拿到玩家名
                player_name, _ = parse_string(payload, 0)
        except (OSError, ValueError, ConnectionError):
            player_name = None

        self.login_attempt_count += 1
        try:
            self._on_login_attempt(player_name, peer)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(f'处理登录尝试回调时出错: {type(exc).__name__}: {exc}')

        kick_text = self._kick_factory()
        reason = json.dumps({'text': kick_text}, ensure_ascii=False)
        try:
            conn.sendall(pack_packet(0x00, encode_string(reason)))
            # 等待对端先关闭，避免本机侧产生 TIME_WAIT 残留
            conn.settimeout(self._linger_timeout)
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
        except (OSError, ValueError, ConnectionError):
            pass

    def _handle_legacy(self, conn: socket.socket) -> None:
        """兼容 1.6 及更早版本的传统查询（首字节 0xFE）。"""
        status = self._status_factory(-1)
        players = status.get('players') if isinstance(status.get('players'), dict) else {}
        version = status.get('version') if isinstance(status.get('version'), dict) else {}
        motd = component_to_text(status.get('description'))
        text = '\u00a71\x00{}\x00{}\x00{}\x00{}\x00{}'.format(
            version.get('protocol', -1),
            version.get('name', ''),
            motd,
            players.get('online', 0),
            players.get('max', 0),
        )
        data = text.encode('utf-16-be')
        try:
            conn.sendall(b'\xff' + encode_ushort(len(text)) + data)
            time.sleep(0.1)
        except (OSError, ValueError):
            pass
