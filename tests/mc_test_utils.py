"""测试辅助：端口分配、假客户端登录、日志收集、MCDR 桩对象。"""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import List, Optional, Tuple

from idle_guard.mcproto import (
    ConnReader,
    encode_string,
    encode_ushort,
    encode_varint,
    pack_packet,
    parse_string,
)


def free_port() -> int:
    """取一个当前空闲的 TCP 端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.1) -> bool:
    """轮询等待条件成立，超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 被测对象中间态可能抛异常，忽略继续等
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001
        return False


def login_attempt(host: str, port: int, name: str = 'Steve', protocol: int = 769,
                  timeout: float = 5.0) -> dict:
    """模拟一个 Minecraft 客户端尝试登录，返回服务端给出的断开原因（已解析为 JSON）。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        handshake = (
            encode_varint(protocol) + encode_string(host) + encode_ushort(port) + encode_varint(2)
        )
        sock.sendall(pack_packet(0x00, handshake))
        sock.sendall(pack_packet(0x00, encode_string(name)))
        reader = ConnReader(sock)
        packet_id, payload = reader.read_packet()
        assert packet_id == 0x00, f'期望登录断开包（0x00），实收 0x{packet_id:02X}'
        text, _ = parse_string(payload, 0)
        return json.loads(text)


def reason_text(reason: dict) -> str:
    """从断开原因 JSON 里取出纯文本。"""
    if isinstance(reason.get('text'), str):
        return reason['text']
    return json.dumps(reason, ensure_ascii=False)


class CollectingLogger(logging.Logger):
    """把日志收集到内存里，便于断言。"""

    def __init__(self, name: str = 'idle_guard_test'):
        super().__init__(name, logging.DEBUG)
        self.records: List[Tuple[str, str]] = []
        self.propagate = False

    def _record(self, level: str, msg, *args, **kwargs) -> None:  # noqa: D401
        try:
            self.records.append((level, str(msg) % args if args else str(msg)))
        except (TypeError, ValueError):
            self.records.append((level, str(msg)))

    def debug(self, msg, *args, **kwargs):  # type: ignore[override]
        self._record('debug', msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):  # type: ignore[override]
        self._record('info', msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):  # type: ignore[override]
        self._record('warning', msg, *args, **kwargs)

    warn = warning

    def error(self, msg, *args, **kwargs):  # type: ignore[override]
        self._record('error', msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):  # type: ignore[override]
        self._record('exception', msg, *args, **kwargs)

    def has(self, keyword: str) -> bool:
        return any(keyword in text for _, text in self.records)

    def text(self) -> str:
        return '\n'.join(text for _, text in self.records)


class StubMCDR:
    """按 Supervisor 实际用到的接口伪造一个 PluginServerInterface。"""

    def __init__(self) -> None:
        self.logger = CollectingLogger()
        self.running = True
        self.started_up = True
        self.said: List[str] = []
        self.told: List[Tuple[str, str]] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.reloaded: List[str] = []
        self.mcdr_language = 'zh_cn'
        #: MCDR 的 EXIT_AFTER_STOP 标记（服务端一启动 MCDR 就会打上它）
        self.exit_after_stop = True
        #: 通过 execute 发给服务端的命令（音效就是这么做出来的）
        self.executed: List[str] = []
        #: 插件数据目录（None = 像"拿不到目录"那样，只留内存不落盘）
        self.data_folder: Optional[str] = None

    # ---- 被 Supervisor 调用的接口 ----
    def get_data_folder(self) -> str:
        if self.data_folder is None:
            raise RuntimeError('这个测试桩没有数据目录')
        return self.data_folder

    def get_mcdr_config(self) -> dict:
        return {}

    def get_mcdr_language(self) -> str:
        return self.mcdr_language

    def is_server_running(self) -> bool:
        return self.running

    def is_server_startup(self) -> bool:
        return self.running and self.started_up

    def say(self, text: str) -> None:
        self.said.append(text)

    def tell(self, player: str, text: str) -> None:
        self.told.append((player, text))

    def execute(self, command: str) -> None:
        self.executed.append(command)

    def start(self) -> None:
        self.start_calls += 1
        self.running = True
        self.started_up = False

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    def reload_plugin(self, plugin_id: str) -> None:
        self.reloaded.append(plugin_id)

    def set_exit_after_stop_flag(self, flag_value: bool) -> None:
        self.exit_after_stop = bool(flag_value)

    # ---- 断言辅助 ----
    def said_text(self) -> str:
        return '\n'.join(self.said)
