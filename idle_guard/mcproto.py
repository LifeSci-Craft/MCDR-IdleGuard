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
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 单个数据包的最大长度（协议规定 2^21-1，这里留一点余量做防御）
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
    """按 [长度 VarInt][包 ID VarInt][负载] 打包一个数据包。"""
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """从 bytes 的 offset 处解析 VarInt，返回 (值, 新的 offset)。"""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError('VarInt 数据不完整')
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 35:
            raise ValueError('VarInt 过长')
    return result, offset


def parse_string(data: bytes, offset: int = 0) -> Tuple[str, int]:
    """从 bytes 的 offset 处解析 String，返回 (字符串, 新的 offset)。"""
    length, offset = decode_varint(data, offset)
    if length < 0 or offset + length > len(data):
        raise ValueError('String 数据不完整')
    return data[offset:offset + length].decode('utf-8', errors='replace'), offset + length


def component_to_text(component: Any) -> str:
    """把聊天组件（str / dict / list）压平成纯文本，用于日志和展示。"""
    if component is None:
        return ''
    if isinstance(component, str):
        return component
    if isinstance(component, (int, float, bool)):
        return str(component)
    if isinstance(component, list):
        return ''.join(component_to_text(item) for item in component)
    if isinstance(component, dict):
        pieces: List[str] = []
        if 'text' in component:
            pieces.append(component_to_text(component['text']))
        elif 'translate' in component:
            pieces.append(str(component.get('translate', '')))
        for key in ('with', 'extra'):
            if key in component:
                pieces.append(component_to_text(component[key]))
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

    def read_packet(self) -> Tuple[int, bytes]:
        """读取一个完整数据包，返回 (包 ID, 负载字节)。"""
        length = self.read_varint()
        if length <= 0 or length > MAX_PACKET_LENGTH:
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
            sample_names = [
                item.get('name', '') for item in (players.get('sample') or [])
                if isinstance(item, dict)
            ]

            # 顺手完成一次 ping/pong 以量测延迟，失败也不影响结果
            try:
                nonce = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
                sock.sendall(pack_packet(0x01, encode_long(nonce)))
                pong_id, _ = reader.read_packet()
                if pong_id == 0x01:
                    latency_ms = (time.monotonic() - started) * 1000.0
            except (OSError, ValueError, ConnectionError):
                pass

            return PingResult(
                online=True,
                players_online=int(players.get('online', -1)),
                players_max=int(players.get('max', -1)),
                sample=sample_names,
                version_name=str(version.get('name', '')),
                protocol=int(version.get('protocol', -1)),
                motd=component_to_text(raw.get('description')),
                latency_ms=latency_ms,
                raw=raw,
            )
    except (OSError, ValueError, ConnectionError) as exc:
        return PingResult(online=False, error=f'{type(exc).__name__}: {exc}')


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口上是否有进程在监听（TCP 层探测，不涉及 Minecraft 协议）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def can_bind(host: str, port: int) -> bool:
    """测试本机能否绑定该端口（用于判断真实服务端是否已经彻底释放端口）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()
