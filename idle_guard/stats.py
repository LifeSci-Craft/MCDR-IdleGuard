"""休眠 / 唤醒的累计统计（`!!ig stats`）。

轻量：一个 json 文件 + 几个计数。用途是让管理员能量化"这插件到底干了多少活"：
休眠了几次、总共关了多少小时（折算成省下的内存 / 电费的口径由你自己定）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional, Tuple

STATS_FILE = 'stats.json'


class Stats:
    """累计计数（落盘，插件重载与 MCDR 重启都不丢）。"""

    def __init__(self, path: Optional[str]) -> None:
        self._path = path
        self._since = time.time()
        self._servers: Dict[str, Dict[str, float]] = {}
        #: 内存里记"当前这次休眠从什么时候开始"，唤醒时结算成 sleep_seconds
        self._sleeping_since: Dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ 读写
    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:  # noqa: BLE001 - 统计文件坏了不该影响插件运行
            return
        if not isinstance(data, dict):
            return
        since = data.get('since')
        if isinstance(since, (int, float)) and since > 0:
            self._since = float(since)
        servers = data.get('servers')
        if isinstance(servers, dict):
            for name, values in servers.items():
                if not isinstance(name, str) or not isinstance(values, dict):
                    continue
                self._servers[name] = {
                    'sleep': _number(values.get('sleep')),
                    'wake': _number(values.get('wake')),
                    'sleep_seconds': _number(values.get('sleep_seconds')),
                }

    def save(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, 'w', encoding='utf-8') as handle:
                json.dump({'since': self._since, 'servers': self._servers},
                          handle, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 计数
    def _entry(self, name: str) -> Dict[str, float]:
        return self._servers.setdefault(name, {'sleep': 0.0, 'wake': 0.0, 'sleep_seconds': 0.0})

    def note_sleep(self, name: str) -> None:
        """记一次成功休眠，并开始给这一段计时。"""
        self._entry(name)['sleep'] += 1
        self._sleeping_since[name] = time.monotonic()
        self.save()

    def note_wake(self, name: str) -> float:
        """记一次成功唤醒，顺手结算上一段休眠时长；返回这段时长（秒）。"""
        self._entry(name)['wake'] += 1
        seconds = self.sleeping_seconds(name)
        self._sleeping_since.pop(name, None)
        self._entry(name)['sleep_seconds'] += seconds
        self.save()
        return seconds

    def sleeping_seconds(self, name: str) -> float:
        """当前这一段（还在进行中的）休眠已经持续了多久；没有则 0。"""
        since = self._sleeping_since.get(name)
        return max(0.0, time.monotonic() - since) if since else 0.0

    def snapshot(self) -> Tuple[float, Dict[str, Dict[str, float]]]:
        """(统计起点时间戳, {子服: {sleep, wake, sleep_seconds}})。"""
        return self._since, {name: dict(values) for name, values in self._servers.items()}


def _number(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


def format_duration(seconds: float) -> str:
    """把秒数写成人类看得懂的样子：`3d 4h` / `2h 15m` / `45m` / `12s`。"""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m'
    return f'{secs}s'
