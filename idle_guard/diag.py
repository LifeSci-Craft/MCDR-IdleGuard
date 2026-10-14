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

from .supervisor import Supervisor

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
    state, detail = sup.bus_bind_state()
    if state == 'ok':
        out.append((tr('diag.bus.listen', address=sup.bus_bind), RColor.gray))
    elif state == 'auto_no_bus':
        out.append((tr('diag.bus.none.auto', server=detail or '?'), RColor.yellow))
    elif state == 'auto_nothing':
        # 不负责任何子服的实例不启动总线属于正常，用灰色而不是黄色（别把"用不上"说成"出错了"）
        out.append((tr('diag.bus.none.watch'), RColor.gray))
    else:
        out.append((tr('diag.bus.invalid', value=detail or '?'), RColor.yellow))

    out.extend(_peer_rows(sup))
    return out


def _peer_rows(sup: Supervisor, *, when_empty: str = 'diag.peer.none') -> List[Row]:
    """对端健康度（**最近一次探测的结果**，不主动探测 —— 那是心跳的活儿）。"""
    tr = sup.tr
    health: Dict[str, dict] = sup.peer_health()
    if not health:
        return [(tr(when_empty), RColor.gray)]
    now = time.monotonic()
    out: List[Row] = []
    for address, info in sorted(health.items()):
        instance = str(info.get('instance') or '') or tr('diag.peer.unknown_instance')
        if info.get('ok'):
            age = max(0, int(now - float(info.get('at') or now)))
            out.append((tr('diag.peer.ok', address=address, instance=instance, seconds=age),
                        RColor.green))
        else:
            out.append((tr('diag.peer.fail', address=address, instance=instance,
                           error=str(info.get('error') or '?')), RColor.red))
    return out


def _velocity_rows(sup: Supervisor) -> List[Row]:
    """`!!ig check` 里的 velocity 同步状态：读了哪个文件、读到几个子服。

    没有这一行时，"为什么子服清单没同步"只能靠猜（是没找到文件、路径填错了，还是关掉了）。
    """
    tr = sup.tr
    info = sup.velocity_status()
    state = str(info.get('state') or 'unknown')
    if state == 'ok':
        return [(tr('check.velocity.ok', path=info.get('path'), count=info.get('count')),
                 RColor.gray)]
    if state == 'disabled':
        return [(tr('check.velocity.disabled'), RColor.gray)]
    if state == 'missing_path':
        return [(tr('check.velocity.missing_path', path=info.get('path')), RColor.yellow)]
    if state == 'empty':
        return [(tr('check.velocity.empty', path=info.get('path')), RColor.yellow)]
    if state == 'not_found':
        return [(tr('check.velocity.not_found', paths=', '.join(info.get('candidates') or [])),
                 RColor.yellow)]
    if state == 'error':
        return [(tr('check.velocity.error'), RColor.yellow)]
    return []


def check_rows(supervisor: Supervisor) -> List[Row]:
    """`!!ig check` 的内容：配置自检结果 + 对端最近一次探测。

    和 `!!ig diag` 的区别：diag 是"现在是什么状态"，check 是"**配置**有没有毛病" ——
    后者不看运行时状态，所以它给的每一条都是能直接照做的。
    """
    tr = supervisor.tr
    problems = supervisor.config_warnings()
    out: List[Row] = []
    if problems:
        out.append((tr('check.header', count=len(problems)), RColor.yellow))
        out.extend((f'  {index}. {text}', RColor.yellow)
                   for index, text in enumerate(problems, start=1))
        out.append((tr('check.hint'), RColor.gray))
    else:
        out.append((tr('check.all_ok'), RColor.green))
    # 口令指纹：两边连不上时，"口令不一致"和"那台实例没运行"表现一样，给个能对照的东西
    password_fingerprint = supervisor.bus_token_fingerprint()
    if password_fingerprint:
        out.append((tr('check.bus.token', fingerprint=password_fingerprint), RColor.gray))
    out.extend(_velocity_rows(supervisor))
    out.append((tr('check.peer.header'), RColor.white))
    out.extend(_peer_rows(supervisor, when_empty='check.peer.none'))
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
    if getattr(view, 'auto_sleep', True):
        # 配着自动休眠却没人负责它 —— 这条配置永远生效不了，必须点出来
        return tr('diag.server.reason.watch_unowned')
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
