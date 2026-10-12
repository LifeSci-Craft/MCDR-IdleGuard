"""编号寻址 / Tab 补全 / 休眠二次确认（用户 2026-10-10 提的三项需求）。

- 每个子服有编号（`!!ig status` 第一列），`!!ig sleep 1` 这种写法要能用；
- MCDR 后台控制台的 Tab 补全直接读命令树的 suggests 回调，所以这里直接验那条路径；
- `!!ig sleep` 只登记待确认，`!!ig confirm` 才真的关服（超时作废、`!!ig cancel` 可撤）。
"""
from __future__ import annotations

import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.supervisor import Supervisor
from mc_test_utils import StubMCDR, free_port

SERVERS = ('lobby', 'survival', 'creative')


class Source:
    def __init__(self, console: bool = True, player: str | None = None) -> None:
        self.is_console = console
        self.player = player
        self.replies: list = []
        self.widgets: list = []

    def reply(self, text) -> None:
        self.widgets.append(text)
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True


@pytest.fixture
def supervisor():
    config = Config(
        instance_name='survival',
        language='zh_cn',
        confirm_timeout=30.0,
        check_interval=600.0,
        idle_timeout=3600.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[ServerEntry(name=name, host='127.0.0.1', port=free_port(), role='self')
                 for name in SERVERS],
    )
    instance = Supervisor(StubMCDR(), config, plugin_version='9.9.9')
    yield instance
    instance.shutdown()


# --------------------------------------------------------------------------- #
# 编号寻址
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('token, expected', [
    ('1', 'lobby'), ('2', 'survival'), ('3', 'creative'),
    ('survival', 'survival'), ('SURVIVAL', 'survival'), ('Creative', 'creative'),
])
def test_number_and_name_both_resolve(supervisor, token, expected):
    assert cmd_mod.resolve_server(supervisor, token) == expected


@pytest.mark.parametrize('token', ['0', '4', '9', '', 'nope', 'surviva'])
def test_out_of_range_or_unknown_tokens_are_rejected(supervisor, token):
    assert cmd_mod.resolve_server(supervisor, token) is None


def test_numbering_follows_config_order(supervisor):
    assert cmd_mod.server_numbers(supervisor) == {'lobby': 1, 'survival': 2, 'creative': 3}


def test_sleep_accepts_a_number(supervisor):
    """`!!ig sleep 1` 要能落到 lobby 上（这里只验到"登记待确认"这一步）。"""
    source = Source()
    cmd_mod._cmd_action(source, supervisor, 'sleep', '1')
    pending, _expired = supervisor.take_pending_action('控制台')
    assert pending is not None and pending.server == 'lobby', source.replies


# --------------------------------------------------------------------------- #
# Tab 补全（MCDR 控制台走的就是这条路径）
# --------------------------------------------------------------------------- #

def test_suggestions_offer_numbers_and_names(supervisor):
    assert cmd_mod.server_suggestions(supervisor) == ['1', '2', '3'] + list(SERVERS)


@pytest.mark.parametrize('action', ['sleep', 'wake', 'lock', 'unlock'])
def test_argument_node_suggests_every_target(supervisor, action):
    node = cmd_mod.build_tree(supervisor, '!!ig')
    suggestions = node._entry_generate_suggestions(Source(), f'!!ig {action} ')
    commands = [suggestion.command for suggestion in suggestions]
    assert commands == [f'!!ig {action} {target}' for target in ('1', '2', '3') + SERVERS]


def test_root_completion_lists_subcommands(supervisor):
    node = cmd_mod.build_tree(supervisor, '!!ig')
    commands = [s.command for s in node._entry_generate_suggestions(Source(), '!!ig ')]
    for expected in ('!!ig status', '!!ig sleep', '!!ig confirm', '!!ig cancel', '!!ig reload'):
        assert expected in commands, commands


def test_status_takes_no_argument(supervisor):
    """用户要求：`!!ig status` 一次列出全部，不再有"只看一个子服"的用法。"""
    root = cmd_mod.build_tree(supervisor, '!!ig')
    status_node = next(child for child in root.get_children() if 'status' in child.literals)
    assert not status_node.has_children(), 'status 不该再有子服参数'


def test_console_completer_path_returns_targets(supervisor):
    """用 MCDR 自己的 CommandManager 走一遍 —— 后台控制台的补全就是调它。"""
    import contextlib
    import logging

    from mcdreforged.command.command_manager import CommandManager
    from mcdreforged.plugin.plugin_registry import PluginCommandHolder

    class FakePluginManager:
        @contextlib.contextmanager
        def with_plugin_context(self, plugin):
            yield

    class FakeMcdrServer:
        logger = logging.getLogger('fake-mcdr')
        plugin_manager = FakePluginManager()

    manager = CommandManager(FakeMcdrServer())
    with manager.start_command_register() as register:
        register(PluginCommandHolder(plugin=object(), node=cmd_mod.build_tree(supervisor, '!!ig'),
                                     allow_duplicates=False))

    def complete(text: str) -> set:
        return {suggestion.command for suggestion in manager.suggest_command(text, Source())}

    assert complete('!!ig ') >= {'!!ig status', '!!ig sleep', '!!ig wake', '!!ig confirm', '!!ig reload'}
    assert complete('!!ig sleep ') == {'!!ig sleep 1', '!!ig sleep 2', '!!ig sleep 3',
                                       '!!ig sleep lobby', '!!ig sleep survival', '!!ig sleep creative'}


# --------------------------------------------------------------------------- #
# 二次确认
# --------------------------------------------------------------------------- #

def test_confirm_is_one_shot(supervisor):
    source = Source()
    cmd_mod._cmd_action(source, supervisor, 'sleep', '2')
    assert supervisor.take_pending_action('控制台')[0] is not None
    cmd_mod._cmd_confirm(source, supervisor)
    # 第二次 confirm 已经没有待确认的了
    again = Source()
    cmd_mod._cmd_confirm(again, supervisor)
    assert any('没有等你确认' in line for line in again.replies)


def test_pending_is_per_source(supervisor):
    """控制台的待确认不该被玩家的 confirm 顺手执行掉。"""
    console = Source()
    player = Source(console=False, player='Steve')
    cmd_mod._cmd_action(console, supervisor, 'sleep', '1')
    assert supervisor.take_pending_action('Steve')[0] is None
    assert supervisor.take_pending_action('控制台')[0] is not None


def test_expired_pending_is_refused(supervisor):
    source = Source()
    supervisor._config.confirm_timeout = 5.0
    cmd_mod._cmd_action(source, supervisor, 'sleep', '1')
    supervisor._pending['控制台'].deadline = time.monotonic() - 1     # 让它过期
    cmd_mod._cmd_confirm(source, supervisor)
    assert any('失效' in line for line in source.replies), source.replies


def test_cancel_drops_the_pending_request(supervisor):
    source = Source()
    cmd_mod._cmd_action(source, supervisor, 'sleep', '1')
    cmd_mod._cmd_cancel(source, supervisor)
    assert any('已取消' in line for line in source.replies), source.replies
    after = Source()
    cmd_mod._cmd_confirm(after, supervisor)
    assert any('没有等你确认' in line for line in after.replies)


def test_chat_confirm_prompt_carries_buttons(supervisor):
    """游戏内给 [确认休眠] / [取消] 两个按钮，点一下就能走完。"""
    source = Source(console=False, player='Steve')
    cmd_mod._cmd_action(source, supervisor, 'sleep', '1')
    widget = source.widgets[-1]
    payload = str(widget.to_json_object())
    assert f'!!ig confirm' in payload and f'!!ig cancel' in payload, payload
    assert 'green' in payload, payload
