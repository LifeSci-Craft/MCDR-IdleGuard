"""休眠 / 唤醒的累计统计（`!!ig stats`）。

轻量：一个 json 文件 + 几个计数。用途是让管理员能量化"这插件到底干了多少活"：
休眠了几次、总共关了多少小时（折算成省下的内存 / 电费的口径由你自己定）。
"""
from __future__ import annotations

import json
import os
import threading
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
        #: 串行化 save()：休眠动作线程与轮询线程都可能调它（以前无锁，可能互相截断）
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ 读写
    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:  # noqa: BLE001 - 统计文件坏了不该影响插件运行
            # 损坏的文件先挪到一边：以前是直接忽略，于是下一次 save() 会把
            # "也许还能人工抢救"的内容覆盖掉，而且不留任何痕迹说明它坏过。
            self._quarantine()
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

    def _quarantine(self) -> None:
        """把读不动的统计文件改名留证（`stats.json.corrupt`），绝不阻断插件运行。"""
        if not self._path:
            return
        try:
            os.replace(self._path, f'{self._path}.corrupt')
        except OSError:
            pass

    def save(self) -> None:
        """原子写盘：先写临时文件再 `os.replace` 整份替换。

        以前是直接 `'w'` 截断写 —— 崩溃/断电正好落在写入中途就留下半个 JSON，
        而 load 端读不动就**整份丢弃**（这个文件里有累计休眠时长，丢了等于历史清零）；
        两个线程同时 save 还会互相截断。现在最坏情况只是"这一次的计数没写进去"。
        """
        if not self._path:
            return
        with self._lock:
            try:
                payload = {'since': self._since, 'servers': self._servers}
                tmp_path = f'{self._path}.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._path)
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
