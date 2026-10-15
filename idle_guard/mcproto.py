"""
Minecraft Java 版协议的最小实现（只覆盖本插件需要的部分）

包含三块内容：
1. VarInt / String / Long 等基础类型的编码与解析；
2. 状态查询（Server List Ping, SLP）客户端 —— 用于探测子服务器是否在线、在线人数、MOTD；
3. 服务端侧的最小数据包构造 —— 供假服务端（fakeserver.py）回复状态查询、在登录阶段断开连接。

协议稳定性说明（本项目依赖的两处报文格式）：
- SLP 自 1.7（协议号 4）起格式固定，1.7 ~ 1.21.x 全部通用；
- 登录阶段断开包（Login Disconnect，login state 客户端方向包 ID 0x00）的 Reason 字段
  在所有版本中都是「JSON 字符串」，连接建立后立即发送即可让客户端显示断开原因。
  本项目已用 1.21.4 客户端实测通过（见 README「验证方式」）。

本模块不依赖任何第三方库。
"""
from __future__ import annotations

import json
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 单个数据包的最大长度。协议里长度前缀是 VarInt，能表达到 2^21-1 以上，但服务端实现
#: （以及本插件的读取侧）都把上限放在这里；写入侧也必须拿同一个常量挡住，否则会出现
#: "自己打出来的包自己解不开"。
MAX_PACKET_LENGTH = 2 ** 21 - 1

#: 状态查询里握手包 next_state = 1，登录是 2
HANDSHAKE_NEXT_STATE_STATUS = 1
HANDSHAKE_NEXT_STATE_LOGIN = 2


# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #

def encode_varint(value: int) -> bytes:
    """把整数编码为 Minecraft 的 VarInt（有符号 32 位，负数按补码处理）。"""
    if value < 0:
        value += 1 << 32
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def encode_string(text: str) -> bytes:
    """编码 Minecraft 的 String 字段：VarInt 长度 + UTF-8 内容。"""
    raw = text.encode('utf-8')
    return encode_varint(len(raw)) + raw


def encode_ushort(value: int) -> bytes:
    return struct.pack('>H', value & 0xFFFF)


def encode_long(value: int) -> bytes:
    return struct.pack('>q', value)


def pack_packet(packet_id: int, payload: bytes = b'') -> bytes:
    """按 [长度 VarInt][包 ID VarInt][负载] 打包一个数据包。

    出站也校验长度：读取侧有 MAX_PACKET_LENGTH 上限，写入侧以前一个检查都没有 ——
    负载一旦变大（MOTD、聊天组件变长）就会打出"我们自己都解析不了"的包，
    而且失败是静默的（对端只会断开）。
    """
    body = encode_varint(packet_id) + payload
    if len(body) > MAX_PACKET_LENGTH:
        raise ValueError(f'数据包过长（{len(body)} 字节 > 上限 {MAX_PACKET_LENGTH}）')
    return encode_varint(len(body)) + body


def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """从 bytes 的 offset 处解析 VarInt，返回 (值, 新的 offset)。

    按协议只接受**最多 5 字节 / 32 位**。以前判的是 `shift > 35`，于是 6 字节的畸形串
    能解出大于 2^32 的"正数" —— 与 encode 端的有符号语义不对称，而且这种值会被后面
    当成长度或下标继续用（隐患）。现在：超过 5 字节、或第 5 个字节还带超出 32 位的数据位，
    一律直接报错。
    """
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError('VarInt 数据不完整')
        byte = data[offset]
        offset += 1
        if shift == 28 and (byte & 0x7F) > 0x0F:
            # 第 5 个字节只允许贡献 4 个数据位（32 位整数的最后 4 位）
            raise ValueError('VarInt 超出 32 位')
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 28:
            raise ValueError('VarInt 过长（超过 5 字节）')
    return result, offset


def parse_string(data: bytes, offset: int = 0) -> Tuple[str, int]:
    """从 bytes 的 offset 处解析 String，返回 (字符串, 新的 offset)。"""
    length, offset = decode_varint(data, offset)
    if length < 0 or offset + length > len(data):
        raise ValueError('String 数据不完整')
    return data[offset:offset + length].decode('utf-8', errors='replace'), offset + length


#: 聊天组件的最大展开深度。原版组件的嵌套通常不到 5 层，给 32 层足够宽松；
#: 而畸形报文可以嵌套上千层，让 `component_to_text` 直接 RecursionError。
MAX_COMPONENT_DEPTH = 32


def _as_int(value: Any, default: int = -1) -> int:
    """把报文里的字段转成 int；类型不对（null / 列表 / 对象 / inf）时退回 default。

    以前直接 `int(...)`：`players.online: null` 这类畸形报文会抛 **TypeError**，
    而它不在 `status_ping` 的 except 列表里 —— 从 `_wait_ready` 那条路径直接漏进唤醒线程，
    状态永久停在"启动中"（服务端其实早就开好了）。
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def component_to_text(component: Any, depth: int = 0) -> str:
    """把聊天组件（str / dict / list）压平成纯文本，用于日志和展示。

    `depth` 是内部参数（调用方不用传）：超过 `MAX_COMPONENT_DEPTH` 就不再往下递归，
    免得畸形报文把栈打爆。
    """
    if depth > MAX_COMPONENT_DEPTH:
        return ''
    if component is None:
        return ''
    if isinstance(component, str):
        return component
    if isinstance(component, (int, float, bool)):
        return str(component)
    if isinstance(component, list):
        return ''.join(component_to_text(item, depth + 1) for item in component)
    if isinstance(component, dict):
        pieces: List[str] = []
        if 'text' in component:
            pieces.append(component_to_text(component['text'], depth + 1))
        elif 'translate' in component:
            pieces.append(str(component.get('translate', '')))
        for key in ('with', 'extra'):
            if key in component:
                pieces.append(component_to_text(component[key], depth + 1))
        return ''.join(pieces)
    return str(component)


# --------------------------------------------------------------------------- #
# 读取器
# --------------------------------------------------------------------------- #

class ConnReader:
    """带缓冲地从 socket 读取 Minecraft 数据包。"""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    def _fill(self, size: int) -> None:
        while len(self._buf) < size:
            chunk = self._sock.recv(max(1024, size - len(self._buf)))
            if not chunk:
                raise ConnectionError('连接被对端关闭')
            self._buf.extend(chunk)

    def read_exact(self, size: int) -> bytes:
        self._fill(size)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def peek(self, size: int = 1) -> bytes:
        """窥探接下来的若干字节而不消费（用于识别 0xFE 传统查询）。"""
        try:
            self._fill(size)
        except (ConnectionError, OSError):
            return b''
        return bytes(self._buf[:size])

    def read_varint(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.read_exact(1)[0]
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 35:
                raise ValueError('VarInt 过长')

    def read_packet(self, max_length: int = MAX_PACKET_LENGTH) -> Tuple[int, bytes]:
        """读取一个完整数据包，返回 (包 ID, 负载字节)。

        ``max_length`` 是**这一包**的长度上限。默认仍是协议上限 2 MiB，但对外且未鉴权的
        读取路径（假服务端的第一条握手）会传一个小得多的值 —— 只信长度前缀就等于让任何
        能连上端口的人要求我们准备 2 MiB 缓冲（见 fakeserver.MAX_CLIENT_PACKET_LENGTH）。
        """
        length = self.read_varint()
        if length <= 0 or length > max_length:
            raise ValueError(f'非法的包长度: {length}')
        data = self.read_exact(length)
        packet_id, offset = decode_varint(data, 0)
        return packet_id, data[offset:]


# --------------------------------------------------------------------------- #
# 状态查询（SLP）客户端
# --------------------------------------------------------------------------- #

@dataclass
class PingResult:
    """一次状态查询的结果。online 表示端口上确实有一个 Minecraft 服务端在响应。"""

    online: bool = False
    players_online: int = -1
    players_max: int = -1
    sample: List[str] = field(default_factory=list)
    version_name: str = ''
    protocol: int = -1
    motd: str = ''
    latency_ms: float = 0.0
    raw: dict = field(default_factory=dict)
    error: str = ''

    @property
    def marker(self) -> Optional[dict]:
        """假服务端写入的自定义标记（真实服务端不会有）。"""
        marker = self.raw.get('_idle_guard') if isinstance(self.raw, dict) else None
        return marker if isinstance(marker, dict) else None

    @property
    def is_fake(self) -> bool:
        return self.marker is not None


def status_ping(host: str, port: int, timeout: float = 3.0) -> PingResult:
    """对 host:port 做一次状态查询。

    任何异常都会转成 online=False 的结果，不会向外抛出（轮询场景下不希望线程崩掉）。
    "任何"包括 OverflowError：端口越界（配置里写错）时 `create_connection` 抛的是它，
    而不是 OSError —— 漏掉它就会从 `_wait_ready` 那条路径直接漏进唤醒线程。
    """
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # 握手包：协议号填 -1（服务端不会校验），next_state = 1
            handshake = (
                encode_varint(-1)
                + encode_string(host)
                + encode_ushort(port)
                + encode_varint(HANDSHAKE_NEXT_STATE_STATUS)
            )
            sock.sendall(pack_packet(0x00, handshake))
            sock.sendall(pack_packet(0x00))  # Status Request：空负载

            reader = ConnReader(sock)
            packet_id, payload = reader.read_packet()
            if packet_id != 0x00:
                return PingResult(online=True, error=f'期望状态响应包，收到 0x{packet_id:02X}')

            text, _ = parse_string(payload, 0)
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                return PingResult(online=True, error=f'MOTD JSON 解析失败: {exc}', raw={})
            if not isinstance(raw, dict):
                raw = {}

            latency_ms = (time.monotonic() - started) * 1000.0
            players = raw.get('players') if isinstance(raw.get('players'), dict) else {}
            version = raw.get('version') if isinstance(raw.get('version'), dict) else {}
            # `sample` 不一定是列表（畸形报文里可以是数字 / 对象 / null）：
            # 直接迭代非列表会抛 TypeError，与下面的 int() 是同一类问题
            raw_sample = players.get('sample')
            if not isinstance(raw_sample, list):
                raw_sample = []
            sample_names = [
                item.get('name', '') for item in raw_sample
                if isinstance(item, dict)
            ]

            # 顺手完成一次 ping/pong 以量测延迟，失败也不影响结果
            try:
                nonce = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
                sock.sendall(pack_packet(0x01, encode_long(nonce)))
                pong_id, _ = reader.read_packet()
                if pong_id == 0x01:
                    latency_ms = (time.monotonic() - started) * 1000.0
            except (OSError, ValueError, ConnectionError, OverflowError):
                pass

            return PingResult(
                online=True,
                players_online=_as_int(players.get('online'), -1),
                players_max=_as_int(players.get('max'), -1),
                sample=sample_names,
                version_name=str(version.get('name', '')),
                protocol=_as_int(version.get('protocol'), -1),
                motd=component_to_text(raw.get('description')),
                latency_ms=latency_ms,
                raw=raw,
            )
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 这里刻意是 Exception 而不是 `(OSError, ValueError, ConnectionError)`：
        # 本函数的契约是"任何异常都转成 online=False"。畸形状态 JSON 能造出 TypeError
        # （players.online 是 null）、RecursionError（description 嵌套上千层）等，
        # 而它们以前会从这个唯一的出口漏出去 —— 调用方 `_wait_ready` 跑在唤醒线程里，
        # 表现是"这台服永远显示启动中、!!ig sleep 被拒、自动休眠失效"，日志里一句都没有。
        return PingResult(online=False, error=f'{type(exc).__name__}: {exc}')


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口上是否有进程在监听（TCP 层探测，不涉及 Minecraft 协议）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, OverflowError, ValueError):
        # OverflowError：端口超出 0..65535（配置里写错一个数）。这里绝不能让它冒出去 ——
        # 它既不是 OSError、也不会被上层的 `except OSError` 接住，一条坏配置就会打断整轮侦察。
        return False


def bind_error(host: str, port: int) -> Optional[Exception]:
    """试着在本机绑定该端口：能绑返回 None，不能绑则返回那个异常（带 errno 便于排查）。

    ⚠️ 这个结果**不能**拿来判断「端口空出来了吗」：端口上只要残留一条 TIME_WAIT
    （每次客户端正常断开都会留下一条），bind 就会报 EADDRINUSE —— 实测在 POSIX 上
    连 SO_REUSEADDR 都救不了（那个标志解决的是"与另一个 socket 共用地址"的协商，
    并不解除 TIME_WAIT 的占用）。所以它只当排查时的诊断信息用，正式判据是
    `port_open`（「还有没有人在监听」，TIME_WAIT 骗不到它）。

    POSIX 上仍然设 SO_REUSEADDR，与 `fakeserver` / `bus` 保持一致；Windows 上不设 ——
    那边的语义是"允许抢一个正在监听的端口"，会让探测恒为"能绑"。

    返回类型从 `Optional[OSError]` 放宽到 `Optional[Exception]`：端口越界（例如配置里写了
    999999）抛的是 **OverflowError**，而它才是"到底哪里不对"的唯一线索 —— 以前它不在
    except 列表里，于是这个函数直接把它抛出去，调用方（`_wait_port_free`）连同整轮侦察一起挂掉。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return None
    except (OSError, OverflowError, ValueError) as exc:
        return exc
    finally:
        sock.close()
