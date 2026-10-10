#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的最小"服务端"：真实监听端口 + 真实应答服务器列表查询。

用途：验证 role=command 那条路径（由 shell 命令启停、不由 MCDR 管理的子服）。
它故意**不带** `_idle_guard` 标记，所以从插件看来就是一个"真实服务端"。

启动：python dummy_server.py --port 30099 --stop-file /path/stop.flag
停止：创建 --stop-file 指定的文件，它会在 0.2 秒内自行退出。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from idle_guard.mcproto import (  # noqa: E402
    ConnReader,
    decode_varint,
    encode_string,
    pack_packet,
    parse_string,
)

STOP_EVENT = threading.Event()


def serve_status(conn: socket.socket, reader: ConnReader, protocol: int) -> None:
    packet_id, _ = reader.read_packet()  # Status Request
    if packet_id != 0x00:
        return
    status = {
        'version': {'name': 'DummyServer', 'protocol': protocol},
        'players': {'max': 20, 'online': 0, 'sample': []},
        'description': {'text': 'DummyServer for tests'},
    }
    conn.sendall(pack_packet(0x00, encode_string(json.dumps(status, ensure_ascii=False))))
    try:
        packet_id, payload = reader.read_packet()
        if packet_id == 0x01 and len(payload) >= 8:
            conn.sendall(pack_packet(0x01, payload[:8]))
    except (OSError, ValueError, ConnectionError):
        pass


def handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(5)
        reader = ConnReader(conn)
        packet_id, payload = reader.read_packet()
        if packet_id != 0x00:
            return
        offset = 0
        protocol, offset = decode_varint(payload, offset)
        _address, offset = parse_string(payload, offset)
        offset += 2  # port
        next_state, offset = decode_varint(payload, offset)
        if next_state == 1:
            serve_status(conn, reader, protocol)
    except (OSError, ValueError, ConnectionError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--stop-file', required=True)
    args = parser.parse_args()

    if os.path.exists(args.stop_file):
        os.remove(args.stop_file)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(16)
    server.settimeout(0.2)
    print(f'DummyServer listening on {args.host}:{args.port}', flush=True)

    while True:
        if os.path.exists(args.stop_file):
            break
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()

    server.close()
    print('DummyServer stopped', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
