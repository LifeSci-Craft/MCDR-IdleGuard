"""从 Velocity 的 `velocity.toml` 里读出子服清单，省得再手抄一遍到插件配置里。

Velocity 配置里的子服就是 `[servers]` 段下的 `名字 = "地址:端口"`：

    [servers]
    Survival = "127.0.0.1:30000"
    Creative = "127.0.0.1:30001"
    try = ["Survival"]        # 这个不是子服，要跳过

不依赖 `tomllib`（MCDR 支持的 Python 版本跨度大，3.11 以下没有这个标准库），
`[servers]` 这一段格式固定，按行解析更省事、也不会引入依赖。
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional, Tuple

#: 要解析的段名
SECTION = 'servers'

#: `名字 = 值`（值可能是字符串、数组、数字，后面再判断）
_ENTRY_RE = re.compile(r'^\s*(?P<name>[A-Za-z0-9_.\-]+)\s*=\s*(?P<value>.+?)\s*$')
#: 值必须是 "host:port" 这种形式
_ADDRESS_RE = re.compile(r'^(?P<host>[^:\s]+):(?P<port>\d{1,5})$')

#: `auto` 模式下相对 MCDR 工作目录的候选路径
DEFAULT_CANDIDATES = (
    'velocity.toml',
    os.path.join('..', 'velocity.toml'),
    os.path.join('..', 'Velocity', 'velocity.toml'),
    os.path.join('Velocity', 'velocity.toml'),
    os.path.join('..', '..', 'Velocity', 'velocity.toml'),
)

#: 表示"关掉这个功能"的写法
OFF_VALUES = ('', 'off', 'none', 'false', 'no', '0')


def find_config(start_dir: str, candidates=DEFAULT_CANDIDATES) -> Optional[str]:
    """在 MCDR 工作目录附近找 velocity.toml；找不到就返回 None（不抛异常）。"""
    for relative in candidates:
        path = os.path.abspath(os.path.join(start_dir, relative))
        if os.path.isfile(path):
            return path
    return None


def parse_servers(path: str) -> Dict[str, Tuple[str, int]]:
    """解析 `[servers]` 段，返回 {子服名: (host, port)}。

    看不懂的行一律跳过（`try`、注释、别的类型都跳过），读不了文件就返回空表。
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}

    entries: Dict[str, Tuple[str, int]] = {}
    inside = False
    for raw in lines:
        line = raw.strip()
        if not inside:
            if line.startswith(f'[{SECTION}]'):
                inside = True
            continue
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):          # 进入下一个段，这一段读完了
            break
        match = _ENTRY_RE.match(line)
        if match is None:
            continue
        value = match.group('value').split('#', 1)[0].strip()
        if len(value) < 2 or value[0] != '"' or value[-1] != '"':
            continue                      # 不是字符串（`try = [...]`、数字、布尔）→ 不是子服
        address = _ADDRESS_RE.match(value[1:-1].strip())
        if address is None:
            continue
        port = int(address.group('port'))
        if 0 < port <= 65535:
            entries[match.group('name')] = (address.group('host'), port)
    return entries


def load_servers(config_path: str, start_dir: str) -> Tuple[Optional[str], Dict[str, Tuple[str, int]]]:
    """找文件 + 解析合成一步：返回 (实际用到的文件路径 或 None, {子服名: (host, port)})。"""
    text = (config_path or '').strip()
    if text.lower() in OFF_VALUES:
        return None, {}
    if text.lower() == 'auto':
        path = find_config(start_dir)
    else:
        candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(text)))
        path = candidate if os.path.isfile(candidate) else None
    if path is None:
        return None, {}
    return path, parse_servers(path)
