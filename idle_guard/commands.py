"""
命令树：`!!ig` / `!!idleguard`

- status               查看所有子服状态（编号 / 角色 / 状态 / 人数 / 空闲计时 / 开关）
- wake   <编号|子服名>  立即唤醒
- sleep  <编号|子服名>  休眠（**需要 `!!ig confirm` 再确认一次**）
- confirm / cancel     确认 / 取消上一次待执行的休眠
- lock / unlock        临时停用 / 恢复自动休眠与唤醒（排查维护时用）
- poll                 立刻侦察一轮（调试用）
- reload               重载插件与配置

子服参数既能写**编号**（`!!ig sleep 1`，编号就是 `!!ig status` 的第一列）也能写名字，
两个都支持 Tab 补全（MCDR 控制台的补全直接读命令树的 suggests 回调）。

⚠️ 一条硬性约束：**MCDR 的命令回调跑在 MCDR 自己的任务线程上**，
所以回调里绝不能做耗时操作（休眠要等服务端停稳、启动要等几十秒）。
凡是会阻塞的动作都丢到后台线程执行，先回一句"正在执行"，完成后再补结果。
（之前把 `sleep_server` 直接写在回调里，MCDR 会报
"TaskExecutor 线程已经 10 秒没有响应了" 并把它标记为卡死。）

界面按既定标准：每屏「顶栏 — 内容 — 底线」，顶栏与底线同宽；控制台按显示宽度补齐，
游戏内按字宽模型补齐并把细节收进浮窗（详见 ui.py）。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, NamedTuple, Optional, Sequence

from mcdreforged.api.all import (
    CommandSource,
    Literal,
    PluginServerInterface,
    RAction,
    RColor,
    RText,
    RTextBase,
    RTextList,
    Text,
)

from . import ui
from .supervisor import MANAGED_ROLES, ServerView, State, Supervisor

#: 命令权限级别（MCDR：0=guest 1=user 2=helper 3=admin 4=owner）
#: 按用户要求，`!!ig` 的**全部**命令默认只有管理员能用（含帮助与 status）。
#: 想放宽只有两条路，都不是改 `permission.yml` 的节点：
#:   ① 想让某条命令对更低等级开放 —— 改这里的数字（会改变所有实例的行为）；
#:   ② 想让某个玩家能用 —— 在 MCDR 的 `permission.yml` 里把他的等级提到 admin
#:      （`permission.yml` 只有玩家到等级的映射，没有节点级权限覆盖）。
#:   注意玩家名**大小写敏感**、精确匹配（MCDR `permission_manager.py`），名字写错会退回 default_level。
PERM_STATUS = 3      # 根命令与 help / status
PERM_CONTROL = 3     # wake / sleep / lock / unlock / confirm / cancel
PERM_ADMIN = 3       # poll / reload

#: 状态 / 角色的颜色表（唯一一份）：绿=正常、黄=正在变动、aqua=本实例在管、红=有问题、灰=信息
_STATE_COLOURS = {
    State.RUNNING.value: RColor.green,
    State.SLEEPING.value: RColor.aqua,
    State.STARTING.value: RColor.yellow,
    State.STOPPING.value: RColor.yellow,
    State.OFFLINE.value: RColor.red,
    State.STOPPED.value: RColor.red,
    State.UNKNOWN.value: RColor.gray,
}
_ROLE_COLOURS = {
    'self': RColor.aqua,
    'command': RColor.aqua,
    'watch': RColor.gray,
}

#: (帮助行的键, 能不能裸跑)
_HELP_ROWS = (
    ('status', True),
    ('wake', False),
    ('sleep', False),
    ('confirm', True),
    ('lock', False),
    ('poll', True),
    ('reload', True),
)

#: 列之间的间隔：控制台 2 个显示列；游戏内 8 个 1/4 字母宽（≈2 个字母）
_GAP = 2
_GAP_GLYPH = 8

#: 状态表最后一列（开关）能直接点的状态 -> (动作, 文案键, 颜色)
_SWITCH_ACTIONS = {
    State.RUNNING.value: ('sleep', 'switch_sleep', RColor.red),
    State.SLEEPING.value: ('wake', 'switch_wake', RColor.green),
    State.OFFLINE.value: ('wake', 'switch_wake', RColor.green),
    State.STOPPED.value: ('wake', 'switch_wake', RColor.green),
}


class Cell(NamedTuple):
    """表格里的一格：文字 + 颜色 + 浮窗 + 点击（后两者只在游戏内有效）。"""

    text: str
    colour: RColor = RColor.white
    hover: Optional[str] = None
    #: 点击后直接执行的命令（游戏内）
    click: Optional[str] = None
    #: 点击后填进输入框的命令（游戏内，给"还要补参数"的命令用）
    fill: Optional[str] = None


# --------------------------------------------------------------------------- #
# 子服寻址（编号 / 名字）
# --------------------------------------------------------------------------- #

def server_names(supervisor: Supervisor) -> List[str]:
    """按配置顺序列出子服名 —— 这个顺序就是 `!!ig status` 里的编号顺序。"""
    return [entry.name for entry in supervisor.config.servers]


def server_numbers(supervisor: Supervisor) -> Dict[str, int]:
    return {name: index for index, name in enumerate(server_names(supervisor), start=1)}


def server_suggestions(supervisor: Supervisor) -> List[str]:
    """Tab 补全的候选：编号在前、名字在后（两种写法都能用）。"""
    names = server_names(supervisor)
    return [str(index) for index in range(1, len(names) + 1)] + names


def resolve_server(supervisor: Supervisor, token: str) -> Optional[str]:
    """把命令参数解析成子服名：支持编号（`1`）与名字（不区分大小写）。"""
    token = (token or '').strip()
    if not token:
        return None
    names = server_names(supervisor)
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(names):
            return names[index - 1]
        return None
    for name in names:
        if name == token:
            return name
    for name in names:
        if name.lower() == token.lower():
            return name
    return None


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _source_name(source: CommandSource, supervisor: Supervisor) -> str:
    """取命令来源的名字。

    注意：控制台来源（ConsoleCommandSource）没有 `player` 属性，不能直接写 source.player。
    """
    player = getattr(source, 'player', None)
    return str(player) if player else supervisor.tr('command.source.console')


def _spawn(source: CommandSource, name: str, worker) -> None:
    """在后台线程里执行耗时动作，完成后由 worker 自己 reply。

    MCDR 的 CommandSource.reply 是线程安全的（控制台写日志、玩家走 tell），
    所以可以直接在后台线程里回复。
    """
    def run() -> None:
        try:
            worker()
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须自己吞掉并告知玩家
            try:
                source.reply(f'§c{type(exc).__name__}: {exc}')
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=run, daemon=True, name=f'IdleGuard-Cmd-{name}').start()


def _is_chat(source: CommandSource) -> bool:
    """是不是"游戏内"那一份渲染（决定补齐方式与要不要挂浮窗/按钮）。"""
    return not getattr(source, 'is_console', False)


def _fill_text(command: str) -> str:
    """把命令里的参数占位切掉，留给"填进输入框"用（`!!ig wake <子服>` -> `!!ig wake `）。"""
    for marker in ('[', '<'):
        index = command.find(marker)
        if index != -1:
            return command[:index]
    return command


def _h(tr, key: str, **kwargs) -> str:
    """取一条浮窗文案并手工断行（客户端只在空格处折行，中文必须自己断）。"""
    return ui.wrap_hover(tr(key, **kwargs).split('\n'))


def _table(cells: Sequence[Sequence[Cell]], *, chat: bool) -> List[RTextBase]:
    """把若干行格子排成表格：控制台按显示宽度，游戏内按字宽模型。

    列宽 = 该列最宽的一格 + 一个固定间隔（否则中文列之间会贴在一起）。
    """
    columns = list(zip(*cells))
    if chat:
        widths = [max(ui.glyph_width(cell.text) for cell in column) + _GAP_GLYPH
                  for column in columns]
    else:
        widths = [max(ui.display_width(cell.text) for cell in column) + _GAP
                  for column in columns]

    lines: List[RTextBase] = []
    for row in cells:
        parts: List[RTextBase] = []
        for index, cell in enumerate(row):
            if index == len(row) - 1:                      # 最后一列不补齐
                padded = cell.text
            else:
                padded = (ui.pad_glyph(cell.text, widths[index]) if chat
                          else ui.pad(cell.text, widths[index]))
            node = RText(padded, cell.colour)
            if chat:
                if cell.hover:
                    node.set_hover_text(cell.hover)
                if cell.click:
                    node.set_click_event(RAction.run_command, cell.click)
                elif cell.fill:
                    node.set_click_event(RAction.suggest_command, cell.fill)
            parts.append(node)
        lines.append(RTextList(*parts))
    return lines


def _screen(source: CommandSource, supervisor: Supervisor, body: Sequence[RTextBase],
            hint: Optional[str] = None) -> None:
    """顶栏 — 内容 — 底线：顶栏与底线**同宽**，宽度按本屏最宽的一行现算。

    底线中间的提示就是这一屏的用法说明；控制台那版没有提示，是一条素线。
    无论是名字还是提示，两侧都至少留 3 个 `=` —— 否则最宽的那行会把装饰挤没。
    """
    chat = _is_chat(source)
    title = ui.title_text(supervisor.plugin_version)
    hint_text = hint if (chat and hint) else ''
    width = max(
        ui.frame_width(chat, [line.to_plain_text() for line in body]),
        ui.frame_cells(title.to_plain_text(), chat) + 6,
        ui.frame_cells(hint_text, chat) + 6,
    )

    source.reply(ui.frame_line(title, width, chat=chat))
    for line in body:
        source.reply(line)
    if hint_text:
        source.reply(ui.frame_line(RText(hint_text, RColor.gray), width, chat=chat))
    else:
        source.reply(ui.frame_line(RText(''), width, chat=chat, spacer=False))


# --------------------------------------------------------------------------- #
# 帮助页
# --------------------------------------------------------------------------- #

def _line_help(source: CommandSource, supervisor: Supervisor, prefix: str) -> None:
    tr = supervisor.tr
    chat = _is_chat(source)
    rows: List[List[Cell]] = []
    for key, _runs_bare in _HELP_ROWS:
        command = tr(f'command.help.cmd.{key}', prefix=prefix)
        description = tr(f'command.help.desc.{key}')
        hover = _h(tr, f'command.help.hover.{key}', prefix=prefix)
        rows.append([Cell(command, RColor.aqua, hover), Cell(description, RColor.gray, hover)])

    if not chat:
        _screen(source, supervisor, _table(rows, chat=False))
        return

    # 游戏内：命令段可点（能裸跑的直接执行、要参数的填进输入框），
    # 说明与中间的留白一律把命令填进输入框（与参考实现同一个取舍）
    for (key, runs_bare), row in zip(_HELP_ROWS, rows):
        command, hover = row[0].text, row[0].hover or ''
        fill = _fill_text(command)
        row[0] = Cell(command, RColor.aqua, hover, click=command if runs_bare else None,
                      fill=None if runs_bare else fill)
        row[1] = Cell(row[1].text, RColor.gray, hover, fill=fill)
    _screen(source, supervisor, _table(rows, chat=True), tr('command.screen.help_hint'))


# --------------------------------------------------------------------------- #
# 状态页
# --------------------------------------------------------------------------- #

def _players_text(view: ServerView) -> str:
    if view.players < 0:
        return '?'
    if view.players_max > 0:
        return f'{view.players}/{view.players_max}'
    return str(view.players)


def _idle_hover_lines(view: ServerView, tr) -> List[str]:
    """空闲信息（连续无人多久 / 阈值 / 还有多久关机）——放在**状态**那一格的浮窗里。

    用户要求：状态表上不再单独占一列（那列长期只有个 `-`，白占宽度），
    改成鼠标移到状态上看。
    """
    if view.always_on:
        # 常驻的子服：空闲多久都不会关，说清这一点比报数字有用
        return tr('hover.idle.always_on').split('\n')
    if view.idle_timeout <= 0:
        return []
    if view.idle_seconds < 0:
        if view.state == State.RUNNING.value:
            return tr('hover.idle.none').split('\n')
        return tr('hover.idle.offline').split('\n')
    # 模板里带 \n：先拆成独立行再交给 wrap_hover（它按显示宽度断行，不认 \n）
    lines = list(tr('hover.idle', idle=int(view.idle_seconds // 60),
                    limit=int(view.idle_timeout // 60),
                    remain=_idle_remain(view, tr)).split('\n'))
    if view.role in MANAGED_ROLES and not view.auto_sleep:
        lines.append(tr('hover.idle.auto_off'))
    elif view.locked:
        lines.append(tr('hover.idle.locked'))
    return lines


def _idle_remain(view: ServerView, tr) -> str:
    left = max(0.0, view.idle_timeout - view.idle_seconds) / 60.0
    return tr('hover.idle.soon') if left <= 0 else tr('hover.idle.remain', left=int(left + 0.5))


def _state_hover(view: ServerView, tr) -> str:
    lines = tr(f'hover.state.{view.state}').split('\n')
    if view.detail:
        lines.append(tr('hover.detail', detail=view.detail))
    lines.extend(_idle_hover_lines(view, tr))
    return ui.wrap_hover(lines)


def _note_cell(view: ServerView, tr, prefix: str) -> Cell:
    """备注：谁在管这台子服（`@实例名`）。细节（异常原因等）都收进浮窗。"""
    if view.instance:
        hover = _h(tr, 'hover.note', instance=view.instance, server=view.name)
        if view.locked:
            return Cell(f'{tr("command.status.locked")}@{view.instance}', RColor.red,
                        _h(tr, 'hover.locked', prefix=prefix) + '\n' + hover)
        return Cell(f'@{view.instance}', RColor.gray, hover)
    return Cell('-', RColor.gray, _h(tr, 'hover.no_owner', server=view.name, prefix=prefix))


def _switch_cell(view: ServerView, index: int, tr, prefix: str, chat: bool) -> Cell:
    """最后一列：一键开关。休眠中的给绿色 [开启]，运行中的给红色 [休眠]。"""
    if view.always_on:
        # 常驻子服不给休眠按钮：点了也只会被拒，不如直接说明它不会休眠
        return Cell(tr('command.status.always_on'), RColor.gray,
                    _h(tr, 'hover.switch.always_on', server=view.name))
    action_info = _SWITCH_ACTIONS.get(view.state)
    if action_info is None:
        if view.state in (State.STARTING.value, State.STOPPING.value):
            return Cell(tr('command.status.switch_busy', state=tr(f'state.{view.state}')),
                        RColor.yellow, _h(tr, 'hover.switch.busy'))
        return Cell('-', RColor.gray, None)
    action, key, colour = action_info
    command = f'{prefix} {action} {index}'
    hover = _h(tr, f'hover.switch.{action}', prefix=prefix, index=index, server=view.name)
    return Cell(tr(f'command.status.{key}'), colour, hover,
                click=command if chat else None)


def _role_cell(view: ServerView, tr) -> Cell:
    """角色列：本实例在管 / 别的实例在管 / 只监视。

    注意：远端实例报上来的 role 是"**它**是这台服的 self"，直接显示成"本实例"会误导
    （状态表里两行都写本实例，其实只有一行是自己管的），所以这里按 `view.local` 区分。
    """
    role = view.role if view.role in _ROLE_COLOURS else 'watch'
    if role in MANAGED_ROLES and not view.local:
        key = 'remote_command' if role == 'command' else 'remote_self'
        return Cell(tr(f'role.{key}'), _ROLE_COLOURS[role], _h(tr, f'hover.role.{key}'))
    return Cell(tr(f'role.{role}'), _ROLE_COLOURS[role], _h(tr, f'hover.role.{role}'))


def status_cells(view: ServerView, index: int, tr, prefix: str, chat: bool) -> List[Cell]:
    players = _players_text(view)
    return [
        Cell(str(index), RColor.gray, _h(tr, 'hover.col_index', prefix=prefix, index=index)),
        Cell(view.name, RColor.white, _h(tr, 'hover.col_server', host=view.host, port=view.port)),
        _role_cell(view, tr),
        Cell(tr(f'state.{view.state}'), _STATE_COLOURS.get(view.state, RColor.gray),
             _state_hover(view, tr)),
        Cell(players, RColor.green if view.players > 0 else RColor.gray, _h(tr, 'hover.players')),
        _note_cell(view, tr, prefix),
        _switch_cell(view, index, tr, prefix, chat),
    ]


def _status_header(tr, prefix: str) -> List[Cell]:
    return [
        Cell(tr('command.status.col_index'), RColor.gray, _h(tr, 'hover.col_index',
                                                              prefix=prefix, index='1')),
        Cell(tr('command.status.col_server'), RColor.gray, _h(tr, 'hover.col_server',
                                                              host='127.0.0.1', port=25565)),
        Cell(tr('command.status.col_role'), RColor.gray, None),
        Cell(tr('command.status.col_state'), RColor.gray, _h(tr, 'hover.col_state')),
        Cell(tr('command.status.col_players'), RColor.gray, _h(tr, 'hover.players')),
        Cell(tr('command.status.col_note'), RColor.gray, _h(tr, 'hover.col_note')),
        Cell(tr('command.status.col_switch'), RColor.gray, _h(tr, 'hover.col_switch')),
    ]


def _cmd_status(source: CommandSource, supervisor: Supervisor, prefix: str = '!!ig') -> None:
    """列出**全部**子服状态（不带子服参数：想看哪台看表格里对应的那一行）。"""
    tr = supervisor.tr
    chat = _is_chat(source)
    views = supervisor.status_views()
    numbers = server_numbers(supervisor)
    names = [name for name in server_names(supervisor) if name in views] or list(views)

    rows = [_status_header(tr, prefix)]
    rows.extend(status_cells(views[name], numbers.get(name, index), tr, prefix, chat)
                for index, name in enumerate(names, start=1))
    _screen(source, supervisor, _table(rows, chat=chat), tr('command.screen.status_hint'))


def _unknown_target(source: CommandSource, supervisor: Supervisor, token: str) -> bool:
    tr = supervisor.tr
    if resolve_server(supervisor, token) is not None:
        return False
    source.reply(RText(tr('command.unknown_server', name=token, count=len(server_names(supervisor))),
                       RColor.red))
    return True


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #

def _notice(source: CommandSource, text: str, colour: RColor) -> None:
    source.reply(RText(text, colour))


#: sleep/wake 的"做了也没意义"状态 -> (文案键, 颜色)
def _precheck_message(action: str, state: str, view: ServerView, tr, name: str):
    """当前状态已经让这个动作没有意义时，给出要说的话；否则返回 None。"""
    busy = tr('command.action.busy', server=name, state=tr(f'state.{state}'))
    if action == 'sleep':
        if view.always_on:
            # 常驻子服：提前拦掉，省得管理员输了 !!ig confirm 才发现关不了
            return tr('action.always_on', server=name), RColor.gray
        if state in (State.SLEEPING.value, State.OFFLINE.value, State.STOPPED.value):
            return tr('action.sleep_not_needed', server=name, state=tr(f'state.{state}')), RColor.gray
        if state in (State.STARTING.value, State.STOPPING.value):
            return busy, RColor.yellow
        if view.players > 0:
            # 有人就不许关（硬限制）：提前拦掉，省得管理员输了确认才发现关不了
            return tr('command.action.sleep_has_players', server=name, count=view.players), RColor.red
        return None
    if state == State.RUNNING.value:
        return tr('action.already_running', server=name), RColor.green
    if state in (State.STARTING.value, State.STOPPING.value):
        return busy, RColor.yellow
    return None


def _precheck_action(source: CommandSource, supervisor: Supervisor, action: str, name: str) -> bool:
    """执行前先侦测一次真实状态，避免"关了还在关、开了还在开"这种没意义的回复。

    数据只在**可信**时（见 Supervisor.live_views）才用来拦截：拿到的是旧快照时宁可放行，
    交给真正负责那台子服的实例去判断（它会给出更准确的结果）。
    """
    if action not in ('sleep', 'wake'):
        return True
    try:
        views, live = supervisor.live_views()
    except Exception:  # noqa: BLE001 - 探测失败不该挡住命令本身
        return True
    view = views.get(name)
    if view is None or name not in live:
        return True
    message = _precheck_message(action, view.state, view, supervisor.tr, name)
    if message is None:
        return True
    text, colour = message
    _notice(source, text, colour)
    return False


def _run_action(source: CommandSource, supervisor: Supervisor, action: str,
                name: str, by: str) -> None:
    """把 sleep / wake 丢到后台线程执行（回调里不能阻塞几十秒）。"""
    tr = supervisor.tr
    if action == 'sleep':
        _notice(source, tr('command.action.sleep_running', server=name), RColor.gray)
    else:
        _notice(source, tr('command.action.wake_running', server=name,
                           eta=int(supervisor.eta_of(name))), RColor.gray)

    def worker() -> None:
        ok, detail = supervisor.request_action(action, name, by=by, wait=True)
        _notice(source, detail, RColor.green if ok else RColor.red)

    _spawn(source, action, worker)


def _ask_confirm(source: CommandSource, supervisor: Supervisor, name: str,
                 prefix: str, by: str) -> None:
    """关服是不可逆的：先登记，等 `!!ig confirm` 再动手（游戏内直接给两个按钮）。"""
    tr = supervisor.tr
    seconds = int(supervisor.confirm_ttl())
    supervisor.set_pending_action(by, 'sleep', name)
    prompt = tr('command.confirm.ask', server=name, seconds=seconds, prefix=prefix)
    if not _is_chat(source):
        _notice(source, prompt, RColor.yellow)
        return
    source.reply(RTextList(
        RText(prompt + ' ', RColor.yellow),
        RText(tr('command.confirm.button_ok'), RColor.green)
        .set_hover_text(_h(tr, 'hover.confirm.ok', prefix=prefix, server=name))
        .set_click_event(RAction.run_command, f'{prefix} confirm'),
        RText(' '),
        RText(tr('command.confirm.button_cancel'), RColor.gray)
        .set_hover_text(_h(tr, 'hover.confirm.cancel'))
        .set_click_event(RAction.run_command, f'{prefix} cancel'),
    ))


def _cmd_action(source: CommandSource, supervisor: Supervisor, action: str, token: str,
                prefix: str = '!!ig') -> None:
    """sleep / wake / lock / unlock。

    锁类操作是瞬时的，直接回结果；休眠要先确认；唤醒与确认后的休眠放到后台线程里做。
    """
    if _unknown_target(source, supervisor, token):
        return
    name = resolve_server(supervisor, token) or token
    by = _source_name(source, supervisor)
    tr = supervisor.tr

    if action in ('lock', 'unlock'):
        ok, detail = supervisor.request_action(action, name, by=by)
        _notice(source, detail, RColor.green if ok else RColor.red)
        return

    if not _precheck_action(source, supervisor, action, name):
        return

    if action == 'sleep':
        _ask_confirm(source, supervisor, name, prefix, by)
        return

    _run_action(source, supervisor, action, name, by)


def _cmd_confirm(source: CommandSource, supervisor: Supervisor, prefix: str = '!!ig') -> None:
    """确认上一次 `!!ig sleep`（超时就失效，必须先重新发起）。"""
    by = _source_name(source, supervisor)
    tr = supervisor.tr
    pending, expired = supervisor.take_pending_action(by)
    if pending is None:
        _notice(source, tr('command.confirm.none', prefix=prefix), RColor.gray)
        return
    if expired:
        _notice(source, tr('command.confirm.expired', seconds=int(supervisor.confirm_ttl()),
                           prefix=prefix), RColor.red)
        return
    # 确认的这段窗口里状态可能已经变了（例如中途有玩家进来 → 现在不许关了），再侦测一次
    if not _precheck_action(source, supervisor, pending.action, pending.server):
        return
    _notice(source, tr('command.confirm.accepted', server=pending.server), RColor.green)
    _run_action(source, supervisor, pending.action, pending.server, by)


def _cmd_cancel(source: CommandSource, supervisor: Supervisor, prefix: str = '!!ig') -> None:
    by = _source_name(source, supervisor)
    tr = supervisor.tr
    pending = supervisor.clear_pending_action(by)
    if pending is None:
        _notice(source, tr('command.confirm.none', prefix=prefix), RColor.gray)
        return
    _notice(source, tr('command.confirm.cancelled', server=pending.server), RColor.green)


def _cmd_poll(source: CommandSource, supervisor: Supervisor) -> None:
    tr = supervisor.tr
    _notice(source, tr('command.poll.running'), RColor.gray)
    started = time.monotonic()

    def worker() -> None:
        try:
            supervisor.poll_once()
            _notice(source, tr('command.poll.done', seconds=f'{time.monotonic() - started:.2f}'),
                    RColor.green)
        except Exception as exc:  # noqa: BLE001
            _notice(source, tr('command.poll.failed', error=f'{type(exc).__name__}: {exc}'), RColor.red)

    _spawn(source, 'poll', worker)


def _cmd_reload(source: CommandSource, supervisor: Supervisor) -> None:
    """重载插件与配置：先回"正在重载"，重载结束后再回一条"重载完成"。

    重载会把当前插件实例卸载再重新加载（本函数所在的模块对象会变成"旧的"，但代码仍在运行），
    所以完成后必须用一个**不依赖新实例**的方式回复 —— 直接对 source 说话即可。
    """
    tr = supervisor.tr
    plugin_id = supervisor.plugin_id
    _notice(source, tr('command.reload.running'), RColor.gray)

    def worker() -> None:
        try:
            supervisor.reload_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            _notice(source, tr('command.reload.failed', error=f'{type(exc).__name__}: {exc}',
                               plugin_id=plugin_id), RColor.red)
            return
        _notice(source, tr('command.reload.done'), RColor.green)

    _spawn(source, 'reload', worker)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #

def build_tree(supervisor: Supervisor, prefix: str) -> Literal:
    """搭出命令树（抽出来是为了能在测试里直接查补全回调）。

    注意：MCDR 调用命令回调时会传入两个参数 (source, context)，
    因此所有 lambda 都必须显式接收第二个参数（context），否则会用 context 覆盖掉默认实参。

    权限：**每个节点都挂了 requires**（含根节点与 help），所以整棵树默认只有管理员能用；
    节点级 requires 同时也让 MCDR 的 permission.yml 能按节点单独放宽。
    """
    def admin(level: int = PERM_CONTROL):
        return lambda src: src.has_permission(level)

    def target(action: str) -> Text:
        """子服参数节点：编号或名字，且挂上补全（MCDR 控制台的 Tab 补全就读这里）。"""
        return (Text('server')
                .suggests(lambda src, ctx, _sup=supervisor: server_suggestions(_sup))
                .runs(lambda src, ctx, _sup=supervisor, _action=action, _p=prefix:
                      _cmd_action(src, _sup, _action, ctx['server'], _p)))

    root = Literal(prefix).requires(admin(PERM_STATUS))
    root.runs(lambda src, ctx, _sup=supervisor, _p=prefix: _line_help(src, _sup, _p))
    root.then(Literal('help').requires(admin(PERM_STATUS)).runs(
        lambda src, ctx, _sup=supervisor, _p=prefix: _line_help(src, _sup, _p)))
    root.then(
        Literal('status').requires(admin(PERM_STATUS))
        .runs(lambda src, ctx, _sup=supervisor, _p=prefix: _cmd_status(src, _sup, _p))
    )
    for action in ('wake', 'sleep', 'lock', 'unlock'):
        root.then(
            Literal(action).requires(admin()).then(target(action))
        )
    root.then(
        Literal('confirm').requires(admin())
        .runs(lambda src, ctx, _sup=supervisor, _p=prefix: _cmd_confirm(src, _sup, _p))
    )
    root.then(
        Literal('cancel').requires(admin())
        .runs(lambda src, ctx, _sup=supervisor, _p=prefix: _cmd_cancel(src, _sup, _p))
    )
    root.then(
        Literal('poll').requires(admin(PERM_ADMIN))
        .runs(lambda src, ctx, _sup=supervisor: _cmd_poll(src, _sup))
    )
    root.then(
        Literal('reload').requires(admin(PERM_ADMIN))
        .runs(lambda src, ctx, _sup=supervisor: _cmd_reload(src, _sup))
    )
    return root


def register(server: PluginServerInterface, supervisor: Supervisor) -> None:
    for prefix in ('!!ig', '!!idleguard'):
        server.register_command(build_tree(supervisor, prefix))
