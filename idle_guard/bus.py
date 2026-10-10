"""
MCDR 实例之间的轻量通信总线（peer bus）

用途：让每个子服的 MCDR 实例能互相"传话"与"下指令"，从而实现跨服通知与控制：
- 通知类（announce）：某台子服休眠/唤醒时，所有实例都在自己管理的服务端里喊一声；
- 查询类（state）：`!!ig status` 需要拿到所有实例的实时状态；
- 指令类（action）：`!!ig wake survival` 这类跨实例操作。

协议：一条连接一条消息（请求-应答式），UTF-8 JSON + 换行分隔，明文 TCP。
连接建立后客户端首先发送 hello 鉴权，服务端校验 token 后才会处理业务消息。

安全性：默认只看本机/内网使用；token 不匹配直接断开。请勿把总线端口暴露到公网。
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Callable, Iterable, List, Optional

#: 单条消息的最大长度，防止被超大报文打爆内存
MAX_MESSAGE_SIZE = 1 << 20


def parse_address(address: str) -> Optional[tuple]:
    """把 "127.0.0.1:25600" 解析为 (host, port)，非法则返回 None。"""
    if not address or not isinstance(address, str):
        return None
    text = address.strip()
    if text.startswith('['):  # IPv6 形式 [::1]:25600
        host, _, port = text.rpartition(']:')
        host = host.lstrip('[')
    else:
        host, _, port = text.rpartition(':')
    if not host or not port.isdigit():
        return None
    return host, int(port)


class Bus:
    """一个极简的请求-应答式 TCP 总线。

    handler(message: dict) -> Optional[dict]
        收到业务消息时调用，返回值会作为应答回给发送方（None 表示不需要应答）。
    """

    def __init__(
        self,
        bind_address: Optional[str],
        token: str,
        handler: Callable[[dict], Optional[dict]],
        logger,
        timeout: float = 3.0,
    ):
        self._bind_address = bind_address
        self._token = token
        self._handler = handler
        self._logger = logger
        self._timeout = timeout

        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> bool:
        if not self._bind_address:
            self._logger.info('未配置总线地址（bus.bind），本实例不接收跨服消息')
            return False
        parsed = parse_address(self._bind_address)
        if parsed is None:
            self._logger.error(f'总线地址格式非法：{self._bind_address!r}，应为 "host:port"')
            return False
        host, port = parsed
        with self._lock:
            if self._running:
                return True
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name != 'nt' and host not in ('0.0.0.0', '::'):
                # POSIX：用 SO_REUSEADDR 绕过 TIME_WAIT。
                # Windows 下**不能**设它 —— 它允许抢占"仍在监听"的端口，
                # 那会让我们以为绑成功了、实际消息全被另一个进程接走（真实踩过），
                # 所以 Windows 上端口被占就直接报错。
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                sock.listen(16)
            except OSError as exc:
                self._logger.error(f'总线绑定 {self._bind_address} 失败：{exc}')
                sock.close()
                return False
            self._sock = sock
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._accept_loop, name='IdleGuard-Bus', daemon=True)
            self._thread.start()
        self._logger.info(f'总线已监听 {self._bind_address}')
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            sock, self._sock = self._sock, None
        self._stop_event.set()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------ 发送

    def send(self, address: str, message: dict, timeout: Optional[float] = None) -> Optional[dict]:
        """向一个地址发送消息并等待应答；失败返回 None（不抛异常）。"""
        parsed = parse_address(address)
        if parsed is None:
            self._logger.debug(f'忽略非法总线地址：{address!r}')
            return None
        host, port = parsed
        deadline = timeout if timeout is not None else self._timeout
        try:
            with socket.create_connection((host, port), timeout=deadline) as sock:
                sock.settimeout(deadline)
                reader = sock.makefile('rb')
                self._write(sock, {'type': 'hello', 'token': self._token})
                ack = self._read(reader)
                if not ack or not ack.get('ok'):
                    self._logger.debug(f'{address} 总线鉴权失败：{ack}')
                    return None
                self._write(sock, message)
                return self._read(reader)
        except (OSError, ValueError) as exc:
            self._logger.debug(f'总线消息发往 {address} 失败：{type(exc).__name__}: {exc}')
            return None

    def broadcast(self, addresses: Iterable[str], message: dict, timeout: Optional[float] = None) -> List[dict]:
        """并行地向多个地址广播，返回所有成功的应答。"""
        targets = [addr for addr in dict.fromkeys(addresses) if parse_address(addr)]
        if not targets:
            return []
        results: List[dict] = []
        lock = threading.Lock()

        def worker(addr: str) -> None:
            reply = self.send(addr, message, timeout=timeout)
            if reply is not None:
                with lock:
                    results.append(reply)

        threads = [threading.Thread(target=worker, args=(addr,), daemon=True) for addr in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=(timeout or self._timeout) + 1.0)
        return results

    def broadcast_async(self, addresses: Iterable[str], message: dict) -> None:
        """后台广播，不阻塞调用方（用于通知类消息）。"""
        targets = [addr for addr in dict.fromkeys(addresses) if parse_address(addr)]
        if not targets:
            return

        def worker() -> None:
            try:
                self.broadcast(targets, message)
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(f'后台广播异常：{type(exc).__name__}: {exc}')

        threading.Thread(target=worker, daemon=True, name='IdleGuard-BusBroadcast').start()

    # ------------------------------------------------------------------ 内部实现

    @staticmethod
    def _write(sock: socket.socket, payload: dict) -> None:
        sock.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))

    @staticmethod
    def _read(reader) -> Optional[dict]:
        line = reader.readline(MAX_MESSAGE_SIZE)
        if not line:
            return None
        try:
            data = json.loads(line.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                sock = self._sock
                if not self._running or sock is None:
                    return
            try:
                conn, addr = sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_conn,
                args=(conn, f'{addr[0]}:{addr[1]}'),
                name='IdleGuard-BusConn',
                daemon=True,
            ).start()

    def _handle_conn(self, conn: socket.socket, peer: str) -> None:
        try:
            conn.settimeout(self._timeout)
            reader = conn.makefile('rb')
            hello = self._read(reader)
            if not hello or hello.get('type') != 'hello' or hello.get('token') != self._token:
                self._write(conn, {'ok': False, 'reason': 'token 校验失败'})
                return
            self._write(conn, {'ok': True, 'name': hello.get('name', '')})

            message = self._read(reader)
            if message is None:
                return
            try:
                reply = self._handler(message)
            except Exception as exc:  # noqa: BLE001
                self._logger.error(f'处理来自 {peer} 的总线消息出错：{type(exc).__name__}: {exc}')
                reply = {'ok': False, 'reason': f'{type(exc).__name__}: {exc}'}
            if reply is not None:
                self._write(conn, reply)
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
