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

import hmac
import json
import os
import socket
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

#: 单条消息的最大长度，防止被超大报文打爆内存
MAX_MESSAGE_SIZE = 1 << 20

#: 鉴权（hello）之前允许读入的最大字节数。
#: 以前第一行就直接按 MAX_MESSAGE_SIZE 读：任何人只要能连上总线端口，就能在**还没证明自己
#: 知道口令**的情况下让我们为它准备 1 MiB 的缓冲。hello 只有几十字节，4 KiB 足够；
#: 认证通过之后再放宽到 MAX_MESSAGE_SIZE。
HELLO_MAX_SIZE = 4096

#: 同时处理的总线连接数上限（与假服务端同样的思路）。
#: 每条连接占一个线程，而且在握手超时之前一直占着 —— 不限个数的话，几百条"连上不说话"
#: 的连接就能把线程与句柄堆满（句柄一满会连带把接受循环打挂，EMFILE 真实踩过）。
#: 正常的对端实例只占个位数，所以这个上限只在被扫或被灌的时候起作用。
MAX_CONNECTIONS = 64

#: 单个来源 IP 的连接数上限：防止一台机器把上面的总闸门全占掉。
MAX_CONNECTIONS_PER_IP = 16


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
    value = int(port)
    # 端口必须在 1..65535：以前只查 isdigit，于是 "127.0.0.1:99999" 会被当成合法地址，
    # 一路走到 socket.bind / create_connection 才抛 OverflowError —— 那不是 OSError，
    # 于是既没被调用方接住，也没被 on_load 兜住（配置里一个笔误就能让插件加载失败）。
    if not 1 <= value <= 65535:
        return None
    return host, value


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
        #: 最近一次发往某地址失败的原因（发送成功即清掉）。
        #: 用途：对端"一个应答都没有"时，光说"没人应答"没法排查 —— 把原因捞出来给管理员看。
        self._last_errors: Dict[str, str] = {}

        #: 连接治理：当前连接总数 / 每个来源 IP 的连接数（都在 _lock 里维护）
        self._active = 0
        self._active_by_ip: Dict[str, int] = {}
        #: 上一次为"连接数超限"打日志的时刻（monotonic）：被灌时不能每来一条就写一行
        self._overflow_log_at = 0.0

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
            except (OSError, OverflowError, ValueError) as exc:
                # OverflowError 由"端口超出 0..65535"触发（正常配置校验会先拦住，这里是兜底：
                # 异常绝不允许穿透到 on_load，否则整个插件都加载不起来）
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
            self._active = 0
            self._active_by_ip.clear()
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
        """向一个地址发送消息并等待应答；失败返回 None（不抛异常）。

        ``timeout`` 是**整次往返**的预算，不是每一步各自一份：以前每一步都重新给一份完整超时
        （连接 + 读 hello 应答 + 读业务应答），实际最坏能拖到预算的三倍，于是
        ``broadcast(..., timeout=2)`` 的调用方按 2 秒+1 秒 join 时，worker 往往还没跑完 ——
        应答被漏收，看着像"对端没回"。
        """
        parsed = parse_address(address)
        if parsed is None:
            self._note_error(address, f'地址非法：{address!r}')
            self._logger.debug(f'忽略非法总线地址：{address!r}')
            return None
        host, port = parsed
        budget = timeout if timeout is not None else self._timeout
        expire_at = time.monotonic() + budget

        def remaining() -> float:
            return max(0.1, expire_at - time.monotonic())

        try:
            with socket.create_connection((host, port), timeout=budget) as sock:
                with sock.makefile('rb') as reader:
                    sock.settimeout(remaining())
                    self._write(sock, {'type': 'hello', 'token': self._token})
                    ack = self._read(reader, HELLO_MAX_SIZE)
                    if not ack or not ack.get('ok'):
                        self._note_error(address, f'鉴权失败（对端 token 与本实例不一致？）：{ack}')
                        self._logger.debug(f'{address} 总线鉴权失败：{ack}')
                        return None
                    sock.settimeout(remaining())
                    self._write(sock, message)
                    reply = self._read(reader)
                    if reply is None:
                        self._note_error(address, '连上了但没在超时内收到应答（对端接受线程可能已失效）')
                    else:
                        with self._lock:
                            self._last_errors.pop(address, None)
                    return reply
        except (OSError, ValueError, OverflowError) as exc:
            self._note_error(address, f'{type(exc).__name__}: {exc}')
            self._logger.debug(f'总线消息发往 {address} 失败：{type(exc).__name__}: {exc}')
            return None

    def _note_error(self, address: str, reason: str) -> None:
        with self._lock:
            self._last_errors[address] = reason

    def last_error(self, address: str) -> str:
        """最近一次发往该地址失败的原因（发送成功过就为空）。用于排查"对端没应答"。"""
        with self._lock:
            return self._last_errors.get(address, '')

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
    def _read(reader, limit: int = MAX_MESSAGE_SIZE) -> Optional[dict]:
        """读一行 JSON。``limit`` 是这一行的字节上限（鉴权前用小值，别让未认证方占大缓冲）。"""
        line = reader.readline(limit)
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
            except OSError as exc:
                if self._stop_event.is_set() or not self.running:
                    return      # 正常停止：socket 被 close() 了
                # ⚠️ 意外错误不能让接受线程直接退出：socket 还留在 LISTEN，
                # 对端连得上、数据也发得出去，但**永远等不到应答** ——
                # 在对方看来就是"那个实例不在线"（真实踩过，排查了很久）。
                # 所以这里记一条日志、歇一下继续转。
                self._logger.warning(f'总线接受连接出错（{type(exc).__name__}: {exc}），1 秒后继续')
                time.sleep(1.0)
                continue
            peer = f'{addr[0]}:{addr[1]}'
            if not self._acquire(addr[0]):
                # 超限：**不开线程**，直接关掉。这里必须留日志，否则表现成"对端莫名其妙连不上"，
                # 而本端一声不响（真实踩过的排查困境）。
                self._log_overflow(peer)
                self._close_quietly(conn)
                continue
            threading.Thread(
                target=self._handle_conn,
                args=(conn, peer),
                name='IdleGuard-BusConn',
                daemon=True,
            ).start()

    # ------------------------------------------------------------------ 连接治理

    def _acquire(self, ip: str) -> bool:
        """占一个连接名额（总数 + 每 IP）；超限返回 False。"""
        with self._lock:
            if self._active >= MAX_CONNECTIONS:
                return False
            if self._active_by_ip.get(ip, 0) >= MAX_CONNECTIONS_PER_IP:
                return False
            self._active += 1
            self._active_by_ip[ip] = self._active_by_ip.get(ip, 0) + 1
            return True

    def _release(self, ip: str) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            rest = self._active_by_ip.get(ip, 0) - 1
            if rest > 0:
                self._active_by_ip[ip] = rest
            else:
                self._active_by_ip.pop(ip, None)

    def _log_overflow(self, peer: str) -> None:
        """连接数超限的日志（最多 10 秒一条，免得被灌时把日志刷爆）。"""
        now = time.monotonic()
        with self._lock:
            if now - self._overflow_log_at < 10.0:
                return
            self._overflow_log_at = now
            active = self._active
        self._logger.warning(
            f'总线连接数已达上限 {MAX_CONNECTIONS}（每 IP 上限 {MAX_CONNECTIONS_PER_IP}），'
            f'丢弃来自 {peer} 的连接；当前 {active} 条。'
            f'若这不是被扫，请检查是否有实例陷入重连循环'
        )

    @staticmethod
    def _close_quietly(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    def _token_matches(self, given: str) -> bool:
        """常量时间比较口令。

        用 `!=` 比字符串会在第一个不同的字符处返回，理论上可被计时探出前缀；
        总线上实际很难利用（144 bit 随机串 + 网络抖动），但这属于零成本的加固。
        统一编码成 bytes，顺带避免非 ASCII 口令让 compare_digest 抛 TypeError。
        """
        try:
            return hmac.compare_digest(given.encode('utf-8'), str(self._token).encode('utf-8'))
        except (AttributeError, UnicodeError):
            return False

    def _handle_conn(self, conn: socket.socket, peer: str) -> None:
        ip = peer.rsplit(':', 1)[0]
        try:
            conn.settimeout(self._timeout)
            # 用 with 管住 makefile：以前只 close(conn)，这个文件对象要等到 GC 才释放
            with conn.makefile('rb') as reader:
                # 鉴权之前只允许读 HELLO_MAX_SIZE：未认证的一方不该能让我们准备 1 MiB 缓冲
                hello = self._read(reader, HELLO_MAX_SIZE)
                given = hello.get('token') if isinstance(hello, dict) else None
                if not hello or hello.get('type') != 'hello' \
                        or not isinstance(given, str) or not self._token_matches(given):
                    self._logger.debug(f'{peer} 的总线握手未通过（口令不一致或报文不合法）')
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
        except (OSError, ValueError) as exc:
            # 以前这里完全静默：对端只看到"鉴权失败/没人应答"，本端一句记录都没有，只能抓包
            self._logger.debug(f'与 {peer} 的总线连接中断：{type(exc).__name__}: {exc}')
        finally:
            self._release(ip)
            try:
                conn.close()
            except OSError:
                pass
