"""`!!ig diag` 的内容：把散落在各处的运行信息汇总成几段"一眼能看懂"的结论。

为什么要做这个命令：出问题时（"为什么没人管 X""为什么它还不休眠""总线到底通不通"）
以前只能翻几百行日志，管理员和开发者都答不上来 —— 线上真踩到过。这里把
判定依据、对端健康度、休眠预检的逐条结论摊开。

这个模块**只读**，不碰任何状态流转。
"""
from __future__ import annotations

import platform
import time
from typing import Dict, List, Tuple

from mcdreforged.api.rtext import RColor

from .supervisor import MANAGED_ROLES, Supervisor

#: 一行诊断 = (文本, 颜色)
Row = Tuple[str, RColor]


def rows(supervisor: Supervisor) -> List[Row]:
    """诊断页的全部行（按显示顺序）。"""
    out: List[Row] = []
    out += _identity(supervisor)
    out += _bus(supervisor)
    out += _servers(supervisor)
    out += _sleep_precheck(supervisor)
    out += _startup(supervisor)
    out += _recent(supervisor)
    return out


# --------------------------------------------------------------------------- #
# 各段
# --------------------------------------------------------------------------- #

def _identity(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    managed = sup.managed_names()
    owner = ', '.join(managed) if managed else tr('diag.managed.none')
    return [
        (tr('diag.identity', instance=sup.instance_name or '?', version=sup.plugin_version,
            python=platform.python_version()), RColor.white),
        (tr('diag.claim', reason=sup.claim_reason or tr('diag.claim.unknown')), RColor.gray),
        (tr('diag.managed', servers=owner), RColor.gray),
    ]


def _bus(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    out: List[Row] = []
    if sup.bus_bind:
        out.append((tr('diag.bus.listen', address=sup.bus_bind), RColor.gray))
    else:
        out.append((tr('diag.bus.none'), RColor.yellow))

    health: Dict[str, dict] = sup.peer_health()
    if not health:
        out.append((tr('diag.peer.none'), RColor.gray))
        return out
    now = time.monotonic()
    for address, info in sorted(health.items()):
        instance = str(info.get('instance') or '?')
        if info.get('ok'):
            age = max(0, int(now - float(info.get('at') or now)))
            out.append((tr('diag.peer.ok', address=address, instance=instance, seconds=age),
                        RColor.green))
        else:
            out.append((tr('diag.peer.fail', address=address, instance=instance,
                           error=str(info.get('error') or '?')), RColor.red))
    return out


def _servers(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    views = sup.status_views()
    names = [name for name in sup.server_names() if name in views] or list(views)
    if not names:
        return [(tr('diag.servers.none'), RColor.gray)]
    out: List[Row] = [(tr('diag.servers.header'), RColor.white)]
    for index, name in enumerate(names, start=1):
        view = views[name]
        out.append((tr('diag.server.line',
                       index=index, server=name,
                       role=sup.role_text(view.role),
                       state=sup.state_text(view.state),
                       players=view.players if view.players >= 0 else '-',
                       reason=_server_reason(sup, view)), RColor.gray))
    return out


def _server_reason(sup: Supervisor, view) -> str:
    """这台服的状态是"谁说了算" —— 出问题时这句最有用。"""
    tr = sup.tr
    if view.local:
        return tr('diag.server.reason.local')
    if view.instance and not view.owner_online:
        return tr('diag.server.reason.offline', instance=view.instance)
    if view.instance:
        return tr('diag.server.reason.remote', instance=view.instance)
    return tr('diag.server.reason.watch')


def _sleep_precheck(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    out: List[Row] = []
    for name in sup.managed_names():
        out.append((tr('diag.precheck.header', server=name), RColor.white))
        for passed, text in sup.sleep_checklist(name):
            out.append((f'{"✓" if passed else "✗"} {text}',
                        RColor.green if passed else RColor.yellow))
    return out


def _startup(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    out: List[Row] = []
    for name in sup.server_names():
        samples = sup.startup_samples(name)
        if not samples:
            continue
        shown = ' / '.join(f'{value:.1f}' for value in samples[-5:])
        out.append((tr('diag.startup', server=name, samples=shown,
                       eta=int(sup.eta_of(name))), RColor.gray))
    return out


def _recent(sup: Supervisor) -> List[Row]:
    tr = sup.tr
    events = sup.recent_events()
    if not events:
        return []
    out: List[Row] = [(tr('diag.recent.header'), RColor.white)]
    out.extend((f'  {text}', RColor.gray) for text in events[-6:])
    return out
