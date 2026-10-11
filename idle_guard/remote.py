"""跨实例总线：收到别的实例的请求、把请求转发给负责它的实例、等它出结果。

从 `supervisor.py` 拆出来的（那个文件原本 2000+ 行）。方法都以 `Supervisor` 的成员身份
运行。几条硬约束值得记住：

- 收到 action 请求必须**立刻应答**（休眠要几十秒，请求方只等总线超时），
  真正的执行转后台，请求方靠轮询状态确认；
- 广播请求时**必须排除自己**，否则本实例先回一句"不负责 X"会把真结果盖掉。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .models import ServerView, State


class RemoteMixin:
    def _on_bus_message(self, message: dict) -> Optional[dict]:
        mtype = message.get('type')
        if mtype == 'announce':
            text = str(message.get('text') or '')
            if message.get('kind') == 'tell':
                player = str(message.get('player') or '')
                self._tell_local(player, text)
                return {'ok': True, 'delivered': self._has_player(player)}
            if message.get('kind') == 'sound':
                player = str(message.get('player') or '')
                self._play_local(player, str(message.get('sound') or ''))
                return {'ok': True, 'delivered': self._has_player(player)}
            self._say_local(text)
            return {'ok': True}
        if mtype == 'state':
            return {
                'ok': True,
                'instance': self.instance_name,
                'servers': [v.to_dict() for v in self.local_views()],
            }
        if mtype == 'action':
            return self._handle_remote_action(message)
        return {'ok': False, 'reason': f'未知消息类型 {mtype!r}'}

    def _handle_remote_action(self, message: dict) -> dict:
        """处理别的实例发来的操作请求。

        ⚠️ 必须**立刻应答**：休眠要等服务端停稳 + 端口移交（几十秒），
        而请求方等应答只有总线超时那么久（默认 3 秒）。以前在这里同步执行，
        于是请求方等到超时、什么也没收到（结果就是那句误报"本实例不负责 X"）。
        真正的执行放到后台线程，请求方靠轮询状态确认结果（见 _wait_remote_state）。
        """
        action = message.get('action')
        name = message.get('server')
        by = str(message.get('by') or self.tr('command.source.remote'))
        managed = self._managed.get(name)
        not_managed = (False, self.tr('action.not_managed', server=name))

        if action in ('wake', 'sleep'):
            if managed is None:
                ok, detail = not_managed
            elif managed.action_in_progress:
                ok, detail = True, self.tr('action.accepted')
            elif action == 'sleep':
                # ⚠️ 先**同步**过一遍"现在到底能不能休眠"再转后台：
                # 以前这里不检查、直接回"已受理"，`_can_sleep` 的拒绝只留在本实例日志里，
                # 请求方于是对着状态干等到 `_wait_remote_state` 超时（线上反馈：
                # 玩家在别的服敲了 !!ig sleep，只看到"正在休眠…"，三分钟后才等到"等待超时"，
                # 真正的原因"这台服上还有人"从没回到他那边）。
                allowed, why_not = self.precheck_sleep(name)
                if allowed:
                    ok, detail = self._sleep_in_background(name, reason='remote', by=by)
                else:
                    ok, detail = False, why_not
                    self._logger.info(f'{name}: 拒绝休眠（remote，{by}）—— {why_not}')
            else:
                ok, detail = self.wake_server(name, reason='remote', by=by)
        elif action in ('lock', 'unlock'):
            if managed is None:
                ok, detail = not_managed
            else:
                managed.locked = (action == 'lock')
                if not managed.locked:
                    managed.wake_failures = 0
                ok, detail = True, self.tr('action.locked' if managed.locked else 'action.unlocked',
                                           server=name)
        else:
            ok, detail = False, self.tr('action.unknown_op', action=repr(action))
        return {'ok': ok, 'reason': detail, 'instance': self.instance_name}

    def request_action(self, action: str, server: str, by: str = 'console',
                       wait: bool = False) -> Tuple[bool, str]:
        """执行一个操作（sleep / wake / lock / unlock）。

        ``wait=True`` 时会等到操作真正有结果（服务端停稳 / 启动就绪，或远程实例报出新状态）
        再返回 —— 命令层把它放在后台线程里调用，所以不会卡住 MCDR。
        """
        if server in self._managed:
            if action == 'wake':
                return self.wake_server(server, reason='manual', by=by, blocking=wait)
            if action == 'sleep':
                return self.sleep_server(server, reason='manual', by=by)
            if action in ('lock', 'unlock'):
                managed = self._managed[server]
                if action == 'lock':
                    managed.locked = True
                else:
                    managed.locked = False
                    managed.wake_failures = 0
                return True, self.tr('action.locked' if managed.locked else 'action.unlocked', server=server)
            return False, self.tr('action.unknown_op', action=repr(action))

        # 广播给**别的**实例，只有负责该服的实例会应答 ok。
        # ⚠️ 必须排除自己：否则本实例会先回一句"本实例不负责 X"，
        # 而远端真正执行休眠/唤醒要几十秒、早就超过总线超时了 —— 于是那句误报会盖掉真正的结果
        # （线上真实踩到：`!!ig sleep Creative` 明明生效了，却回"本实例不负责 Creative"）。
        replies = [reply for reply in
                   (self._bus.send(address, {'type': 'action', 'action': action, 'server': server, 'by': by})
                    for address in self._peer_addresses())
                   if reply]
        accepted = next((reply for reply in replies if reply.get('ok')), None)
        if accepted is not None:
            detail = str(accepted.get('reason') or self.tr('action.accepted'))
            if not wait or action in ('lock', 'unlock'):
                return True, detail
            return self._wait_remote_state(server, action, detail)
        if replies:
            # 有实例应答了，但都说自己不负责（比如它那条的 role 是 watch）
            return False, str(replies[0].get('reason') or self.tr('action.failed'))

        # 一个应答都没有：负责它的实例不在线，或者这条没配总线地址
        addresses = self._peer_addresses()
        entry = next((e for e in self._config.servers if e.name == server), None)
        role = entry.role if entry is not None else 'watch'
        # 提示要说对病因：role 已经是 auto 时，再劝人"把 role 改成 auto"是废话
        # （线上真踩到过：管理员照做了，结果提示一字未变）
        hint = self.tr('action.hint_auto' if role == 'auto' else 'action.hint_own',
                       server=server, role=role)
        # 连失败原因一起记：只说"没人应答"没法排查，得看清是连不上、鉴权失败还是对端没回
        last_error = getattr(self._bus, 'last_error', None)
        tried = []
        for address in addresses:
            reason = last_error(address) if callable(last_error) else ''
            tried.append(f'{address}（{reason}）' if reason else address)
        self._logger.warning(self.tr('action.no_owner_none_answered', server=server, action=action,
                                      addresses='、'.join(tried) if tried else '无'))
        if entry is not None and not entry.bus:
            return False, self.tr('action.no_remote_bus', server=server, hint=hint)
        return False, self.tr('action.no_owner', server=server, hint=hint)

    def _wait_remote_state(self, server: str, action: str, accepted_detail: str) -> Tuple[bool, str]:
        """等远程实例上的目标状态出现（或超时）。"""
        timeout = (self._config.startup_timeout if action == 'wake' else self._config.stop_timeout) + 30.0
        wanted = {State.RUNNING.value} if action == 'wake' else {State.SLEEPING.value, State.OFFLINE.value}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            view = self._lookup_view(server)
            if view is not None and view.state in wanted:
                if action == 'wake':
                    return True, self.tr('action.wake_done_simple', server=server)
                return True, self.tr('action.sleep_stopped', server=server)
            time.sleep(2.0)
        return False, self.tr('action.wait_timeout', server=server, seconds=int(timeout))

    def _lookup_view(self, server: str) -> Optional[ServerView]:
        try:
            return self.status_views().get(server)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(f'查询 {server} 状态失败：{type(exc).__name__}: {exc}')
            return None

