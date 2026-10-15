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

#: 单个来源 IP 的连接数上限：一台机器不该把上面的名额全占掉（真人玩家、代理、探测各占一两条）。
MAX_CONNECTIONS_PER_IP = 16

#: 名额用满之后仍然"多收"几条连接的名额。
#: 为什么还要多收：真实玩家撞上满员时，如果只是被关掉，他**既看不到提示、也触发不了唤醒**
#: （而"一连就能唤醒"正是这个插件的全部价值）。所以再放几条进来做**短预算**处理：
#: 读握手的超时缩到 1 秒、发完断开包立刻关，只为了让玩家收到那句话并把唤醒发出去。
#: 用完之后一律走"只记一次尝试 + 记日志 + 关连接"的轻路径，不再开线程。
OVERFLOW_CONNECTIONS = 16

#: 名额用满时，读握手的超时（秒）。正常是 read_timeout（5 秒）。
OVERFLOW_READ_TIMEOUT = 1.0

#: 连接数超限的告警最短间隔（秒）：被扫时不能每来一条连接就写一行日志。
OVERFLOW_WARN_INTERVAL = 10.0

#: 假服务端**允许客户端发来的**单个数据包长度上限。
#: 这是一个直接对外、且握手阶段完全没有鉴权的游戏端口：以前它和别处共用协议上限 2 MiB，
#: 于是任何能连上端口的人都能让我们先准备 2 MiB 的缓冲（按并发上限推算常驻可到 160 MiB）。
#: 这条路径上真正会出现的包都很小：Handshake 里地址最长 255 字节、LoginStart 里名字 16 字节、
#: 1.19+ 的签名数据约 1 KiB，Status Request / Ping 是空的或 8 字节。8 KiB 足够宽松。
MAX_CLIENT_PACKET_LENGTH = 8192

#: 发完登录断开包后等对端关闭的**总**时间预算（秒）：这是整段等待的上限，
#: 不是每次 recv 的预算 —— 后者会被任何一个字节重置，细水长流的客户端能无限期占住名额。
#: 顺便还限制累计读取的字节数（正常客户端收到断开包就不说话了）。
LINGER_MAX_BYTES = 64 * 1024


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
        max_connections_per_ip: int = MAX_CONNECTIONS_PER_IP,
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
        self._max_connections_per_ip = max(1, int(max_connections_per_ip))
        self._overflow_slots = OVERFLOW_CONNECTIONS
        self._overflow_read_timeout = OVERFLOW_READ_TIMEOUT
        #: 当前正在处理的连接数（正常名额 + 溢出名额）
        self._active = 0
        #: 每个来源 IP 当前占用的连接数
        self._active_by_ip: dict = {}
        #: 上一次"连接数超限"告警的时刻（monotonic）
        self._overflow_warn_at = 0.0

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
                # backlog 必须 ≥ 并发上限，否则突发时先撞 backlog：玩家看到的是"连接超时"，
                # 而不是被我们限流（后者至少有日志、有提示，好排查）
                sock.listen(max(64, (self._max_connections + self._overflow_slots) * 2))
            except (OSError, OverflowError, ValueError) as exc:
                # OverflowError：端口超出 0..65535（配置里写错一个数）。它**必须**在这里被接住 ——
                # 本函数的承诺是"端口不可用时返回 False、不抛异常"（端口还被真实服务端占着是正常情况，
                # 由上层在轮询里重试）。漏掉它的话：轮询线程在第一个坏条目上中断整轮侦察，
                # 而抢占端口的后台线程会直接死掉且日志里一句都没有。
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

            peer = f'{addr[0]}:{addr[1]}'
            # 接受成功即清掉上一次的接受错误：这个字段的语义是"最近一次仍未恢复的问题"，
            # 留着几小时前的一次抖动会把 !!ig diag 的读者带偏
            with self._lock:
                self._last_error = ''
                slot = self._acquire_locked(addr[0])
            if not slot:
                # 名额用尽：仍然是"有人在连"，所以**不能**把这次尝试悄悄丢掉 ——
                # 记一次登录尝试（玩家照样能唤醒服务端）、记一条限流日志，再关掉连接。
                self._warn_overflow(peer)
                self._note_login_attempt(None, peer)
                self._close_quietly(conn)
                continue
            if slot == 'overflow':
                self._warn_overflow(peer)

            try:
                threading.Thread(
                    target=self._serve_connection,
                    args=(conn, peer, slot == 'overflow'),
                    name=f'IdleGuard-FakeConn-{self._port}',
                    daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 拿到连接、占好名额之后才失败（资源紧张时 Thread.start 抛
                # RuntimeError: can't start new thread）。两件事都必须做：
                # ① 把名额还回去 —— 否则它会一直泄漏，攒够上限之后真人玩家的连接
                #    就再也进不来（连"已休眠，正在启动"的提示都收不到）；
                # ② **绝不能**让接受线程退出 —— socket 还留在 LISTEN、running 仍是 True，
                #    但再也没人 accept，玩家连接收不到任何回复、也触发不了唤醒。
                # 这正是本循环注释声称已防住的失败模式，以前只给 sock.accept 包了 try。
                self._release(addr[0])
                self._last_error = f'{type(exc).__name__}: {exc}'
                self._logger.warning(
                    f'假服务端接受 {peer} 后启动处理线程失败（{self._last_error}），'
                    f'已回滚并发名额并继续监听'
                )
                self._close_quietly(conn)
                continue

    # ------------------------------------------------------------------ 并发名额

    def _acquire_locked(self, ip: str) -> str:
        """占一个连接名额，返回 'normal' / 'overflow' / ''（已用尽）。调用方需持有 self._lock。"""
        if self._active_by_ip.get(ip, 0) >= self._max_connections_per_ip:
            return ''
        if self._active < self._max_connections:
            slot = 'normal'
        elif self._active < self._max_connections + self._overflow_slots:
            slot = 'overflow'
        else:
            return ''
        self._active += 1
        self._active_by_ip[ip] = self._active_by_ip.get(ip, 0) + 1
        return slot

    def _release(self, ip: str) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            rest = self._active_by_ip.get(ip, 0) - 1
            if rest > 0:
                self._active_by_ip[ip] = rest
            else:
                self._active_by_ip.pop(ip, None)

    def _warn_overflow(self, peer: str) -> None:
        """连接数超限的告警（最短间隔 OVERFLOW_WARN_INTERVAL，免得被灌时刷爆日志）。"""
        now = time.monotonic()
        with self._lock:
            if now - self._overflow_warn_at < OVERFLOW_WARN_INTERVAL:
                return
            self._overflow_warn_at = now
            active = self._active
        self._logger.warning(
            f'假服务端连接数已达上限（正常 {self._max_connections} 条 + 溢出 {self._overflow_slots} 条，'
            f'每 IP {self._max_connections_per_ip} 条）：{peer} 的连接被限流，当前 {active} 条。'
            f'玩家此刻可能收不到提示 —— 若反复出现，检查有没有客户端在反复重连'
        )

    def _note_login_attempt(self, player: Optional[str], peer: str) -> None:
        """记一次连接尝试并通知上层（正常路径与限流路径共用，保证唤醒语义不丢）。"""
        self.login_attempt_count += 1
        try:
            self._on_login_attempt(player, peer)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(f'处理登录尝试回调时出错: {type(exc).__name__}: {exc}')

    def _serve_connection(self, conn: socket.socket, peer: str, short: bool = False) -> None:
        """跑完一条连接，并把并发计数还回去（计数在 `_accept_loop` 里加）。

        ``short=True`` 表示这是"溢出名额"里的连接：只给很短的处理预算，
        目的是让玩家尽快收到提示、并让唤醒照常发出去。
        """
        ip = peer.rsplit(':', 1)[0]
        try:
            self._handle(conn, peer, short=short)
        finally:
            self._release(ip)

    @staticmethod
    def _close_quietly(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    def _handle(self, conn: socket.socket, peer: str, short: bool = False) -> None:
        try:
            # settimeout 同时管住收和发（Python 没有单独的写超时），所以一条连接的
            # 预算 = 读握手 + 发包 + 等对端关闭，都不会无限期挂着。
            # short=True（溢出名额）时把读超时压到 1 秒，别让"连上不说话"的连接长期占位。
            conn.settimeout(self._overflow_read_timeout if short else self._read_timeout)
            reader = ConnReader(conn)
            if reader.peek(1)[:1] == b'\xfe':
                self._handle_legacy(conn)
                return

            packet_id, payload = reader.read_packet(MAX_CLIENT_PACKET_LENGTH)
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
                self._handle_login(conn, reader, peer, short=short)
        except (OSError, ValueError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001 - 假服务端不能因为单个连接异常而崩溃
            self._logger.debug(f'假服务端处理 {peer} 的连接时出错: {type(exc).__name__}: {exc}')
        finally:
            self._close_quietly(conn)

    def _handle_status(self, conn: socket.socket, reader: ConnReader, protocol: int = -1) -> None:
        packet_id, _ = reader.read_packet(MAX_CLIENT_PACKET_LENGTH)
        if packet_id != 0x00:  # Status Request
            return
        status = self._status_factory(protocol)
        conn.sendall(pack_packet(0x00, encode_string(json.dumps(status, ensure_ascii=False))))
        # 可选：回一个 Pong，让客户端的延迟测量正常完成
        try:
            packet_id, payload = reader.read_packet(MAX_CLIENT_PACKET_LENGTH)
            if packet_id == 0x01 and len(payload) >= 8:
                conn.sendall(pack_packet(0x01, payload[:8]))
        except (OSError, ValueError, ConnectionError):
            pass

    def _handle_login(self, conn: socket.socket, reader: ConnReader, peer: str,
                      short: bool = False) -> None:
        player_name: Optional[str] = None
        try:
            packet_id, payload = reader.read_packet(MAX_CLIENT_PACKET_LENGTH)
            if packet_id == 0x00:  # Login Start
                # 字段顺序为 name [+ 后续字段]，只需读取第一个 String 即可拿到玩家名
                player_name, _ = parse_string(payload, 0)
        except (OSError, ValueError, ConnectionError):
            player_name = None

        self._note_login_attempt(player_name, peer)

        kick_text = self._kick_factory()
        reason = json.dumps({'text': kick_text}, ensure_ascii=False)
        try:
            conn.sendall(pack_packet(0x00, encode_string(reason)))
            if short:
                # 溢出名额：发完就走，不等对端关闭（这类连接的存在本身就是压力）
                return
            # 等待对端先关闭，避免本机侧产生 TIME_WAIT 残留。
            # ⚠️ 必须给整段等待一个**绝对**截止时间：以前 `conn.settimeout(linger_timeout)`
            # 是"每次 recv"的预算，任何字节都会把它重置 —— 每 0.5 秒发 1 字节的客户端能
            # 把一条连接无限期占住（实测 12 秒后仍在占名额），攒够并发名额后真人玩家
            # 就收不到"已休眠，正在启动"这句提示了（而那正是假服务端存在的全部价值）。
            deadline = time.monotonic() + self._linger_timeout
            budget = LINGER_MAX_BYTES
            while budget > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                conn.settimeout(max(0.05, left))
                chunk = conn.recv(min(1024, budget))
                if not chunk:
                    break
                budget -= len(chunk)
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
            # 这个长度字段的语义是 **UTF-16 码元数**，不是码点数：非 BMP 字符（emoji 等）
            # 在 UTF-16 里占 2 个码元，而 len(text) 数的是码点 —— 于是 MOTD 里一个 emoji
            # 就会让声明值比实际少 1，按声明读取的 1.6 客户端尾部会少一个字符。
            # 文案可由用户在 config/idle_guard/lang 下覆盖，所以这是用户可触发的。
            units = len(text.encode('utf-16-le')) // 2
            conn.sendall(b'\xff' + encode_ushort(min(units, 65535)) + data)
            time.sleep(0.1)
        except (OSError, ValueError):
            pass
