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

    def __init__(self, path: Optional[str], logger=None) -> None:
        self._path = path
        self._logger = logger
        self._since = time.time()
        self._servers: Dict[str, Dict[str, float]] = {}
        #: 内存里记"当前这次休眠从什么时候开始"，唤醒时结算成 sleep_seconds
        self._sleeping_since: Dict[str, float] = {}
        #: 串行化全部读写：休眠动作线程、唤醒线程、轮询线程与命令线程都会碰这几个容器。
        #: 以前这把锁只保护 save()，于是 `_entry()` 的 `setdefault` 会与 snapshot() /
        #: json.dump 无锁迭代撞上 —— `!!ig stats` 偶发 `dictionary changed size during iteration`，
        #: 而动作线程那份被 save() 的静默 except 吞掉，统计静默不落盘且毫无痕迹。
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ 读写
    def _warn(self, text: str) -> None:
        if self._logger is not None:
            self._logger.warning(text)

    def _debug(self, text: str) -> None:
        if self._logger is not None:
            self._logger.debug(text)

    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - 统计文件坏了不该影响插件运行
            # 损坏的文件先挪到一边：以前是直接忽略，于是下一次 save() 会把
            # "也许还能人工抢救"的内容覆盖掉，而且不留任何痕迹说明它坏过。
            self._warn(f'统计文件读取失败（已隔离为 .corrupt，本次从零开始）：'
                       f'{type(exc).__name__}: {exc}')
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
        """原子写盘：先在锁内浅拷贝，再写临时文件、`os.replace` 整份替换。

        以前是直接 `'w'` 截断写 —— 崩溃/断电正好落在写入中途就留下半个 JSON，
        而 load 端读不动就**整份丢弃**（这个文件里有累计休眠时长，丢了等于历史清零）；
        两个线程同时 save 还会互相截断。现在最坏情况只是"这一次的计数没写进去"。
        """
        if not self._path:
            return
        with self._lock:
            # 锁内只做浅拷贝：json.dump 可能比较慢（磁盘），不该占着锁不让动作线程计数。
            # 浅拷贝足够 —— 内层每个 dict 只在 _entry() 里原地改，而这里要的是"一份稳定的
            # 键与值快照"，浅拷贝已经把它们定下来了。
            payload = {'since': self._since,
                       'servers': {name: dict(values) for name, values in self._servers.items()}}
        try:
            tmp_path = f'{self._path}.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as exc:  # noqa: BLE001
            # 以前这里是 `except Exception: pass`：磁盘满 / 权限不对时统计静默不落盘，
            # 管理员只会看到"数字不涨"，没有任何线索。
            self._debug(f'统计写入失败（这一次的计数没落盘）：{type(exc).__name__}: {exc}')

    # ------------------------------------------------------------------ 计数
    def _entry(self, name: str) -> Dict[str, float]:
        with self._lock:
            return self._servers.setdefault(name, {'sleep': 0.0, 'wake': 0.0,
                                                   'sleep_seconds': 0.0})

    def note_sleep(self, name: str) -> None:
        """记一次成功休眠，并开始给这一段计时。"""
        with self._lock:
            self._entry(name)['sleep'] += 1
            self._sleeping_since[name] = time.monotonic()
        self.save()

    def note_wake(self, name: str) -> float:
        """记一次成功唤醒，顺手结算上一段休眠时长；返回这段时长（秒）。"""
        with self._lock:
            self._entry(name)['wake'] += 1
            seconds = self._ongoing_locked(name)
            self._sleeping_since.pop(name, None)
            self._entry(name)['sleep_seconds'] += seconds
        self.save()
        return seconds

    def settle(self, name: str) -> float:
        """结算当前这一段休眠时长，但**不计**一次唤醒；返回这段时长（秒）。

        为什么需要它：`note_wake` 是**唯一**会 pop `_sleeping_since` 的地方，而"服务端确实
        起来了"并不只经过 `_do_wake` —— 管理员从 MCDR 控制台 / 脚本开服（`on_server_startup`）、
        `role=command` 的子服由状态查询转 RUNNING，都绕过了唤醒动作。少了这个结算入口，
        `_sleeping_since` 里那台服再也不会被清掉，`!!ig stats` 的"已休眠时长"会从那一刻起
        一直涨下去（每小时多算 3600 秒），而且涨的是**已经结束**的那一段。
        """
        with self._lock:
            seconds = self._ongoing_locked(name)
            if name in self._sleeping_since:
                self._sleeping_since.pop(name, None)
                self._entry(name)['sleep_seconds'] += seconds
        if seconds:
            self.save()
        return seconds

    def reconcile(self, sleeping_names) -> None:
        """按"当前确实在休眠"的集合校正 `_sleeping_since`。

        插件重载会重建 Stats，于是**正在进行中**的那一段休眠在内存里没有任何起点：
        `sleeping_seconds` 一直是 0，那一整段都不算进统计。这里给"正在睡但没起点"的子服
        补一个起点（从现在开始算），至少插件重启后的那一段能计上。
        反方向（已经不在睡、但还留着起点）不用在这里处理：那种情况由 settle() 在
        "服务端起来了"的各个入口结算（见 supervisor 里的调用点）。
        """
        names = {name for name in sleeping_names if name}
        with self._lock:
            now = time.monotonic()
            for name in names:
                # 顺带把条目建出来：`stats_snapshot` 只给"已经在 _servers 里的名字"折算进行中
                # 的那一段，不建条目的话正在睡的服在 `!!ig stats` 里根本不显示。
                self._entry(name)
                self._sleeping_since.setdefault(name, now)

    def _ongoing_locked(self, name: str) -> float:
        """调用方需持有 self._lock：当前这一段（还在进行中的）休眠已持续多久。"""
        since = self._sleeping_since.get(name)
        return max(0.0, time.monotonic() - since) if since else 0.0

    def sleeping_seconds(self, name: str) -> float:
        """当前这一段（还在进行中的）休眠已经持续了多久；没有则 0。"""
        with self._lock:
            return self._ongoing_locked(name)

    def snapshot(self) -> Tuple[float, Dict[str, Dict[str, float]]]:
        """(统计起点时间戳, {子服: {sleep, wake, sleep_seconds}})。

        在锁内浅拷贝：命令线程渲染 `!!ig stats` 时，动作线程可能正在为"第一次休眠 / 唤醒"
        的那台服插入新键（`_entry` 的 setdefault），无锁迭代会直接抛
        `dictionary changed size during iteration`。
        """
        with self._lock:
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
