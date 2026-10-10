"""界面标准测试。

用户定的标准（跨项目，见用户记忆「MCDR 插件 / 工具的 UI 统一约定」）：
每一屏都是一个「框」——顶栏印插件名与版本、底线印这一屏的提示，两条**同宽**、
两侧都用金色 `=` 填满（所以提示是 `==== 提示 ====` 这种形式，不是挂在行尾）；
帮助页两列对齐（控制台按显示宽度、游戏内按字宽模型）；
细节收进浮窗，且**浮窗每行 ≤ 32 显示列**（客户端只在空格处折行，中文写成一条会画出屏幕）。
"""
from __future__ import annotations

import json
import os

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import ui
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.supervisor import Supervisor, State
from mc_test_utils import StubMCDR, free_port

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'idle_guard', 'lang')

#: 帮助页的行（与 commands._HELP_ROWS 对齐）
HELP_KEYS = ('status', 'wake', 'sleep', 'confirm', 'lock', 'poll', 'reload')


class Source:
    """一个命令来源桩：is_console 决定走控制台还是游戏内那一份渲染。"""

    def __init__(self, console: bool = True) -> None:
        self.is_console = console
        self.replies: list = []
        self.widgets: list = []

    def reply(self, text) -> None:
        self.widgets.append(text)
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True


class PlayerSource(Source):
    player = 'Steve'

    def __init__(self) -> None:
        super().__init__(console=False)


@pytest.fixture
def supervisor():
    config = Config(
        instance_name='survival',
        language='zh_cn',
        check_interval=600.0,
        idle_timeout=3600.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=[ServerEntry(name='survival', host='127.0.0.1', port=free_port(), role='self'),
                 ServerEntry(name='creative', host='127.0.0.1', port=free_port(), role='watch')],
    )
    instance = Supervisor(StubMCDR(), config, plugin_version='9.9.9')
    yield instance
    instance.shutdown()


def _screens(supervisor):
    """(help 控制台, help 游戏内, status 控制台, status 游戏内) 四屏的逐行文本。"""
    rendered = []
    for source, action in ((Source(), 'help'), (PlayerSource(), 'help'),
                           (Source(), 'status'), (PlayerSource(), 'status')):
        cmd_mod._line_help(source, supervisor, '!!ig') if action == 'help' \
            else cmd_mod._cmd_status(source, supervisor)
        rendered.append(source.replies)
    return rendered


def _width(line: str, console: bool) -> int:
    return ui.display_width(line) if console else ui.glyph_width(line)


# --------------------------------------------------------------------------- #
# 顶栏与底线
# --------------------------------------------------------------------------- #

def test_title_bar_names_the_plugin_and_the_version(supervisor):
    help_console, help_chat, status_console, _status_chat = _screens(supervisor)
    for lines in (help_console, help_chat, status_console):
        title = lines[0]
        assert title.startswith('=') and title.endswith('='), title
        assert 'Idle Guard' in title and 'v9.9.9' in title, title


def test_top_and_bottom_are_the_same_width(supervisor):
    """用户要求：标题栏要跟下方平齐。控制台按列精确相等，游戏内允许 1 格舍入。"""
    help_console, help_chat, status_console, status_chat = _screens(supervisor)
    for lines, console in ((help_console, True), (status_console, True),
                           (help_chat, False), (status_chat, False)):
        top, bottom = _width(lines[0], console), _width(lines[-1], console)
        slack = 0 if console else ui.FILL_UNIT
        assert abs(top - bottom) <= slack, (lines[0], lines[-1], top, bottom)


def test_the_frame_is_at_least_as_wide_as_the_content(supervisor):
    """顶栏/底线不能比正文窄。

    游戏内那份用的是**近似**字宽模型（比例字体），而表格是按"列宽 + 间隔"一格一格补的，
    每格的取整误差会累加，所以这里允许 1 格的舍入余量（视觉上就是差半像素级，
    详见 ui.pad_glyph 的注释）。
    """
    for lines, console in zip(_screens(supervisor), (True, False, True, False)):
        body = max(_width(line, console) for line in lines[1:-1])
        frame = _width(lines[0], console)
        slack = 1 if console else 2 * ui.FILL_UNIT
        assert frame + slack >= body, (lines[0], body)
        # 反向也不能离谱：框比内容宽太多就说明宽度算错了
        assert frame - body <= 3 * ui.FILL_UNIT, (lines[0], body)


def test_bottom_line_centers_its_hint_between_equals(supervisor):
    """用户要求：底线的提示要写成 `==== 提示 ====`，不是挂在行尾。"""
    help_console, help_chat, status_console, status_chat = _screens(supervisor)
    for lines, hint, console in ((help_chat, 'command.screen.help_hint', False),
                                 (status_chat, 'command.screen.status_hint', False)):
        bottom = lines[-1]
        text = supervisor.tr(hint)
        assert text in bottom, bottom
        left, right = bottom.split(text, 1)
        assert left.strip(' ') == '=' * len(left.strip(' ')), left
        assert right.strip(' ') == '=' * len(right.strip(' ')), right
        assert len(left.strip(' ')) >= 2 and len(right.strip(' ')) >= 2, bottom
        assert _width(left, console) - _width(right, console) <= ui.FILL_UNIT, bottom
    # 控制台那版没有提示，是一条素线
    for lines in (help_console, status_console):
        assert set(lines[-1]) == {'='}, lines[-1]


def test_the_widgets_are_real_widgets_not_plain_strings(supervisor):
    """游戏内那一份必须是 RText（否则颜色、浮窗、点击全都会静默丢掉）。"""
    source = PlayerSource()
    cmd_mod._line_help(source, supervisor, '!!ig')
    assert all(hasattr(widget, 'to_json_object') for widget in source.widgets)


# --------------------------------------------------------------------------- #
# 帮助页
# --------------------------------------------------------------------------- #

def test_help_rows_are_aligned_in_two_columns(supervisor):
    help_console, *_rest = _screens(supervisor)
    columns = set()
    for key in HELP_KEYS:
        command = supervisor.tr(f'command.help.cmd.{key}', prefix='!!ig')
        description = supervisor.tr(f'command.help.desc.{key}')
        line = next(line for line in help_console if line.startswith(command))
        columns.add(ui.display_width(line[:line.index(description)]))
    assert len(columns) == 1, f'说明列没对齐：{sorted(columns)}'


def test_help_rows_are_short_and_details_live_on_hover(supervisor):
    help_console, *_rest = _screens(supervisor)
    for line in help_console:
        if not line.startswith('!!ig'):
            continue
        assert '(' not in line and '（' not in line, f'行上不该有括号：{line}'


def test_help_has_a_hover_for_every_row(supervisor):
    for key in HELP_KEYS:
        assert supervisor.tr(f'command.help.hover.{key}'), key
        assert supervisor.tr(f'command.help.cmd.{key}', prefix='!!ig').startswith('!!ig')


# --------------------------------------------------------------------------- #
# 浮窗
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('language', ['zh_cn', 'en_us'])
def test_every_tooltip_line_fits_within_32_columns(language):
    catalog = json.load(open(os.path.join(LANG_DIR, f'{language}.json'), encoding='utf-8'))
    samples = {
        '{prefix}': '!!ig', '{server}': 'Creative', '{name}': 'Creative', '{idle}': '12',
        '{limit}': '60', '{remain}': '还有 48 分钟就休眠', '{left}': '48', '{minutes}': '60',
        '{interval}': '600', '{eta}': '60', '{seconds}': '15', '{count}': '3', '{detail}': 'x',
        '{error}': 'x', '{role}': 'watch', '{code}': '0', '{value}': 'x', '{fallback}': 'en_us',
        '{options}': 'auto, en_us, zh_cn', '{plugin_id}': 'idle_guard', '{what}': 'x',
        '{action}': 'x', '{state}': 'x', '{player}': 'Steve', '{path}': 'x', '{index}': '1',
        '{instance}': 'Creative', '{host}': '127.0.0.1', '{port}': '25565',
    }
    bad = []
    for key, template in catalog.items():
        if not (key.startswith('hover.') or key.startswith('command.help.hover')):
            continue
        filled = template
        for placeholder, value in samples.items():
            filled = filled.replace(placeholder, value)
        for line in filled.split('\n'):
            if ui.display_width(line) > ui.HOVER_WIDTH:
                bad.append((key, ui.display_width(line), line))
    assert not bad, bad


# --------------------------------------------------------------------------- #
# 状态表
# --------------------------------------------------------------------------- #

def _cell_starts(line: str, texts) -> list:
    """每段文本在行内的起始**显示列**。"""
    starts, cursor = [], 0
    for text in texts:
        index = line.find(text, cursor)
        if index < 0:
            return starts
        starts.append(ui.display_width(line[:index]))
        cursor = index + len(text)
    return starts


def _status_rows(supervisor, console=True) -> list:
    source = Source(console)
    cmd_mod._cmd_status(source, supervisor)
    return source.replies


def test_status_has_no_heading_line_and_names_every_column(supervisor):
    """用户要求：去掉顶部的"子服状态"，而每一列都要有表头。"""
    replies = _status_rows(supervisor)
    assert '子服状态' not in replies, replies
    header = replies[1]
    for key in ('index', 'server', 'role', 'state', 'players', 'note', 'switch'):
        assert supervisor.tr(f'command.status.col_{key}') in header, (key, header)
    # 用户要求：空闲不再单独占一列（长期只有个 '-'），信息搬进"状态"的浮窗
    assert 'command.status.col_idle' not in header


def test_status_row_aligns_with_its_header(supervisor):
    replies = _status_rows(supervisor)
    header = replies[1]
    labels = [supervisor.tr(f'command.status.col_{key}') for key in
              ('index', 'server', 'role', 'state', 'players')]
    view = supervisor.status_views()['survival']
    values = ['1', view.name, supervisor.tr(f'role.{view.role}'),
              supervisor.tr(f'state.{view.state}'), cmd_mod._players_text(view)]
    assert _cell_starts(header, labels) == _cell_starts(replies[2], values), (header, replies[2])


def test_first_column_numbers_the_servers_in_config_order(supervisor):
    """用户要求：每个子服都有编号，且编号能直接当命令参数用。"""
    replies = _status_rows(supervisor)
    names = [entry.name for entry in supervisor.config.servers]
    for index, name in enumerate(names, start=1):
        assert any(line.startswith(f'{index}  {name}') for line in replies), (index, name, replies)
        assert cmd_mod.resolve_server(supervisor, str(index)) == name


def test_every_row_shows_the_instance_that_manages_it(supervisor):
    """用户报的 bug：本实例那一行的备注是空的。现在每行都要有 @实例。"""
    replies = _status_rows(supervisor)
    view = supervisor.status_views()['survival']
    assert view.instance, '本实例负责的子服也该带上实例名'
    row = next(line for line in replies if line.startswith('1  '))
    assert f'@{view.instance}' in row, row


def test_switch_column_offers_the_opposite_action(supervisor):
    """用户要求：末尾给个开关 —— 休眠中给绿色 [开启]，运行中给红色 [休眠]，点了就执行。"""
    view = supervisor.status_views()['survival']
    tr = supervisor.tr

    view.state = State.RUNNING.value
    cell = cmd_mod._switch_cell(view, 1, tr, '!!ig', chat=True)
    assert cell.text == tr('command.status.switch_wake') or cell.text == tr('command.status.switch_sleep')
    assert cell.click == '!!ig sleep 1', cell
    assert cell.colour == cmd_mod.RColor.red, '运行中应当给红色的休眠按钮'

    view.state = State.SLEEPING.value
    cell = cmd_mod._switch_cell(view, 1, tr, '!!ig', chat=True)
    assert cell.click == '!!ig wake 1', cell
    assert cell.colour == cmd_mod.RColor.green, '休眠中应当给绿色的开启按钮'

    view.state = State.STARTING.value
    cell = cmd_mod._switch_cell(view, 1, tr, '!!ig', chat=True)
    assert cell.click is None, '正在变动时不该给按钮'


def test_switch_column_is_clickable_in_chat_only(supervisor):
    """控制台点不了按钮，所以那一列在控制台只是文字；游戏内要真的可点。"""
    source = Source(console=True)
    cmd_mod._cmd_status(source, supervisor)
    # survival 还没侦察过（状态未知 → 开关是 '-'），creative 是未运行 → 应当给 [开启]
    assert any('[' in line for line in source.replies if line.startswith('2  ')), source.replies

    chat = PlayerSource()
    cmd_mod._cmd_status(chat, supervisor)
    payloads = [json.dumps(widget.to_json_object(), ensure_ascii=False) for widget in chat.widgets
                if hasattr(widget, 'to_json_object')]
    clickable = [p for p in payloads if '"clickEvent"' in p and 'run_command' in p]
    assert clickable, '游戏内那一份应当有可点击的开关'
    assert any('!!ig wake 2' in p or '!!ig sleep 2' in p for p in clickable), clickable
