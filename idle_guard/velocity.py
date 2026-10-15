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
from typing import Dict, List, Optional, Tuple

#: 要解析的段名
SECTION = 'servers'

#: `名字 = 值`（值可能是字符串、数组、数字，后面再判断）
_ENTRY_RE = re.compile(r'^\s*(?P<name>[A-Za-z0-9_.\-]+)\s*=\s*(?P<value>.+?)\s*$')
#: 段头：TOML 允许方括号里带空白，所以 `[ servers ]` 也是合法的同一段
_SECTION_RE = re.compile(r'^\[\s*' + SECTION + r'\s*\]')
#: 值必须是 "host:port" 这种形式；主机部分允许 IPv6 的方括号写法 `[::1]:30004`
_ADDRESS_RE = re.compile(r'^(?P<host>\[[0-9A-Fa-f:.]+\]|[^:\s]+):(?P<port>\d{1,5})$')

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


def _strip_comment(value: str) -> str:
    """去掉值后面的 `# 注释`，但**引号内**的 `#` 不算注释（TOML 里它是普通字符）。"""
    quote = ''
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = ''
            continue
        if character in ('"', "'"):
            quote = character
        elif character == '#':
            return value[:index]
    return value


def find_config(start_dir: str, candidates=DEFAULT_CANDIDATES) -> Optional[str]:
    """在 MCDR 工作目录附近找 velocity.toml；找不到就返回 None（不抛异常）。"""
    for relative in candidates:
        path = os.path.abspath(os.path.join(start_dir, relative))
        if os.path.isfile(path):
            return path
    return None


def parse_servers_detailed(path: str) -> Tuple[Dict[str, Tuple[str, int]], List[str]]:
    """解析 `[servers]` 段，返回 ({子服名: (host, port)}, 跳过的行原文列表)。

    以前只有 `parse_servers`，被跳过的行**一声不响**地消失：`!!ig check` 只会回显
    "读到 N 个子服"，管理员无法看出有行被丢弃，只能自己数 velocity.toml。
    现在把跳过的行一并交出来（debug 级别列出原文，`!!ig check` 带上计数）。

    跳过规则：注释 / 空行 / 非子服条目（`try = [...]`、数字、布尔）/ 读不懂的地址。
    """
    entries: Dict[str, Tuple[str, int]] = {}
    skipped: List[str] = []
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}, []

    inside = False
    for raw in lines:
        line = raw.strip()
        if not inside:
            if _SECTION_RE.match(line):
                inside = True
            continue
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):          # 进入下一个段，这一段读完了
            break
        match = _ENTRY_RE.match(line)
        if match is None:
            skipped.append(line)
            continue
        value = _strip_comment(match.group('value')).strip()
        # 值必须是字符串：双引号（基本字符串）与单引号（字面字符串）都是合法 TOML，
        # 以前只认双引号，于是 `Survival = '127.0.0.1:30000'` 被静默丢掉。
        quote = value[:1]
        if len(value) < 2 or quote not in ('"', "'") or value[-1:] != quote:
            skipped.append(line)
            continue                  # 不是字符串（`try = [...]`、数字、布尔）→ 不是子服
        address = _ADDRESS_RE.match(value[1:-1].strip())
        if address is None:
            skipped.append(line)
            continue
        host = address.group('host')
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]         # IPv6：socket 接口要的是不带方括号的地址
        port = int(address.group('port'))
        if 0 < port <= 65535:
            entries[match.group('name')] = (host, port)
        else:
            skipped.append(line)
    return entries, skipped


def parse_servers(path: str) -> Dict[str, Tuple[str, int]]:
    """解析 `[servers]` 段，返回 {子服名: (host, port)}（兼容旧调用方）。"""
    return parse_servers_detailed(path)[0]


def load_servers_detailed(config_path: str, start_dir: str
                          ) -> Tuple[Optional[str], Dict[str, Tuple[str, int]], List[str]]:
    """找文件 + 解析合成一步：(实际用到的文件路径 或 None, 子服表, 跳过的行)。"""
    text = (config_path or '').strip()
    if text.lower() in OFF_VALUES:
        return None, {}, []
    if text.lower() == 'auto':
        path = find_config(start_dir)
    else:
        candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(text)))
        path = candidate if os.path.isfile(candidate) else None
    if path is None:
        return None, {}, []
    entries, skipped = parse_servers_detailed(path)
    return path, entries, skipped


def load_servers(config_path: str, start_dir: str) -> Tuple[Optional[str], Dict[str, Tuple[str, int]]]:
    """找文件 + 解析合成一步：返回 (实际用到的文件路径 或 None, {子服名: (host, port)})。"""
    path, entries, _skipped = load_servers_detailed(config_path, start_dir)
    return path, entries
