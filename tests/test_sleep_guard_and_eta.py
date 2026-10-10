"""用户 2026-10-10 追加的四项要求的回归测试。

- **权限**：`!!ig` 的*全部*命令只有管理员能用（含帮助与 `status`），普通玩家会被 MCDR 挡下；
- **ETA**：给玩家报的"预计 xx 秒"要用**上一次实测的启动耗时**，并跨插件重载保留；
- **休眠限制**：**有玩家的服务器不许休眠** —— 手动指令也拒绝，玩家数不可信时同样不放行；
- **文案**：聊天里 `[Idle Guard]` 前缀的颜色必须统一（`§b` 天蓝），不能这条黄那条绿。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from types import SimpleNamespace

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'idle_guard', 'lang')


class PermSource:
    """按权限级别放行的命令来源（模仿 MCDR 的 has_permission）。"""

    def __init__(self, level: int, console: bool = True) -> None:
        self.level = level
        self.is_console = console
        self.replies: list = []

    def reply(self, text) -> None:
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return self.level >= level

    def get_preference(self):
        """MCDR 报错时要读来源的语言偏好（这里给个最小实现）。"""
        return SimpleNamespace(language='zh_cn')


def _make_supervisor(*, data_folder=None, servers=None, **overrides):
    """造一个被测 Supervisor，并返回 (supervisor, stub, config)。"""
    stub = StubMCDR()
    stub.data_folder = data_folder
    config = Config(
        instance_name='survival',
        language='zh_cn',
        check_interval=600.0,
        idle_timeout=60.0,
        min_uptime=0.0,
        grace_after_wake=0.0,
        stop_timeout=1.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=servers or [ServerEntry(name='survival', host='127.0.0.1', port=free_port(),
                                        role='self')],
        **overrides,
    )
    return Supervisor(stub, config, plugin_version='9.9.9'), stub, config


@pytest.fixture
def env(monkeypatch):
    """带可控状态查询的 Supervisor：状态查询返回几人在线由测试指定。"""
    created = []

    def _make(*, online_players=0, sample=(), players=(), role='self', online=True, **overrides):
        def fake_ping(host, port, timeout=3.0):
            if not online:
                return PingResult(online=False, error='模拟：端口无人响应')
            return PingResult(online=True, players_online=online_players,
                              players_max=20, sample=list(sample), motd='模拟服务端')

        monkeypatch.setattr(supervisor_module, 'status_ping', fake_ping)
        supervisor, stub, _config = _make_supervisor(
            servers=[ServerEntry(name='survival', host='127.0.0.1', port=free_port(), role=role,
                                 start_command='echo start', stop_command='echo stop',
                                 working_directory='.')],
            **overrides)
        created.append(supervisor)
        managed = supervisor.state_of('survival')
        managed.state = State.RUNNING
        managed.players.update(players)
        managed.slp = fake_ping('127.0.0.1', 1)
        return SimpleNamespace(sup=supervisor, stub=stub, managed=managed)

    yield _make
    for supervisor in created:
        with contextlib.suppress(Exception):
            supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ① 权限：只有管理员能用
# --------------------------------------------------------------------------- #

def _literal_nodes(node):
    """遍历命令树，只挑出"命令名"节点（Literal）。"""
    if hasattr(node, 'literals'):
        yield node
    for child in node.get_children():
        yield from _literal_nodes(child)


def test_permission_constants_default_to_admin():
    assert (cmd_mod.PERM_STATUS, cmd_mod.PERM_CONTROL, cmd_mod.PERM_ADMIN) == (3, 3, 3)


def test_every_command_requires_admin(env):
    """`!!ig` 的每一条命令（含帮助与 status）都要挂权限要求，且普通玩家过不了。"""
    instance, _stub, _config = _make_supervisor(data_folder=None)
    try:
        root = cmd_mod.build_tree(instance, '!!ig')
        player, helper, admin = PermSource(1), PermSource(2), PermSource(3)
        names = set()
        for node in _literal_nodes(root):
            names |= set(node.literals)
            assert node._requirements, f'{node} 没有挂权限要求'
            for requirement in node._requirements:
                assert requirement.requirement(player) is False, f'{node} 普通玩家能执行'
                assert requirement.requirement(helper) is False, f'{node} helper 能执行'
                assert requirement.requirement(admin) is True, f'{node} 管理员被挡住了'
        assert names == {'!!ig', 'help', 'status', 'wake', 'sleep', 'lock', 'unlock',
                         'confirm', 'cancel', 'poll', 'reload'}, names
    finally:
        instance.shutdown()


def test_admin_only_is_enforced_by_mcdr_itself(env):
    """走 MCDR 自己的命令管理器执行：普通玩家拿不到状态表，管理员可以。"""
    from mcdreforged.command.command_manager import CommandManager
    from mcdreforged.plugin.plugin_registry import PluginCommandHolder

    class FakePluginManager:
        @contextlib.contextmanager
        def with_plugin_context(self, plugin):
            yield

    class FakeMcdrServer:
        logger = logging.getLogger('fake-mcdr')
        plugin_manager = FakePluginManager()

        def translate(self, key, *args, **kwargs):
            return key      # MCDR 用它把"权限不足"翻译成人话，测试里返回键名就够

        def translate(self, key, *args, **kwargs):
            return key      # MCDR 用它把"权限不足"翻译成人话，测试里返回键名就够

    instance, _stub, _config = _make_supervisor()
    try:
        manager = CommandManager(FakeMcdrServer())
        with manager.start_command_register() as register:
            register(PluginCommandHolder(plugin=object(), node=cmd_mod.build_tree(instance, '!!ig'),
                                         allow_duplicates=False))

        player = PermSource(1)
        manager.execute_command('!!ig status', player)
        assert not any('子服' in line for line in player.replies), \
            f'普通玩家竟然看到了状态表：{player.replies}'

        admin = PermSource(4)
        manager.execute_command('!!ig status', admin)
        assert any('子服' in line for line in admin.replies), f'管理员没拿到状态表：{admin.replies}'
    finally:
        instance.shutdown()


# --------------------------------------------------------------------------- #
# ③ 预计开服时间 = 上一次实测的启动耗时
# --------------------------------------------------------------------------- #

def test_eta_is_the_last_measured_startup_time(tmp_path):
    instance, _stub, config = _make_supervisor(data_folder=str(tmp_path))
    try:
        assert instance.eta_of('survival') == config.fallback_startup_seconds, '还没量过就该用兜底值'
        instance._remember_startup_seconds('survival', 12.4)
        assert instance.eta_of('survival') == 12.4
        assert os.path.isfile(os.path.join(str(tmp_path), 'startup_seconds.json'))
    finally:
        instance.shutdown()


def test_eta_survives_a_plugin_reload(tmp_path):
    """插件重载会重建 Supervisor —— 上次的实测值必须从盘上读回来。"""
    first, _stub, _config = _make_supervisor(data_folder=str(tmp_path))
    try:
        first._remember_startup_seconds('survival', 11.0)
    finally:
        first.shutdown()

    second, _stub2, config = _make_supervisor(data_folder=str(tmp_path))
    try:
        assert second.eta_of('survival') == 11.0, '重载后 ETA 退回了兜底值'
        assert second.eta_of('survival') != config.fallback_startup_seconds
    finally:
        second.shutdown()


def test_this_session_measurement_wins_over_the_saved_one(tmp_path):
    instance, _stub, _config = _make_supervisor(data_folder=str(tmp_path))
    try:
        instance._remember_startup_seconds('survival', 30.0)
        instance.state_of('survival').last_startup_seconds = 8.0
        assert instance.eta_of('survival') == 8.0
    finally:
        instance.shutdown()


def test_broken_stats_file_does_not_break_loading(tmp_path):
    (tmp_path / 'startup_seconds.json').write_text('{ 这不是 json', encoding='utf-8')
    instance, _stub, config = _make_supervisor(data_folder=str(tmp_path))
    try:
        assert instance.eta_of('survival') == config.fallback_startup_seconds
    finally:
        instance.shutdown()


def test_eta_falls_back_when_there_is_no_data_folder():
    instance, _stub, config = _make_supervisor(data_folder=None)
    try:
        instance._remember_startup_seconds('survival', 9.9)
        assert instance.eta_of('survival') == 9.9          # 不落盘也要能用
    finally:
        instance.shutdown()
    assert config.fallback_startup_seconds > 0


# --------------------------------------------------------------------------- #
# ④ 有玩家的服务器不许休眠
# --------------------------------------------------------------------------- #

def test_empty_server_can_sleep(env):
    world = env(online_players=0)
    assert world.sup._can_sleep(world.managed) == (True, '')


def test_server_with_players_is_refused(env):
    world = env(online_players=2, sample=('Steve', 'Alex'), players=('Steve', 'Alex'))
    allowed, why = world.sup._can_sleep(world.managed)
    assert not allowed
    assert 'Steve' in why and 'Alex' in why and '2' in why, why


def test_refusal_also_happens_at_the_sleep_entry(env):
    """不只是 _can_sleep 拒绝 —— 真正的 sleep_server 必须什么都不做。"""
    world = env(online_players=1, sample=('Steve',), players=('Steve',))
    ok, detail = world.sup.sleep_server('survival', reason='manual', by='测试')
    assert not ok, detail
    assert '不能休眠' in detail, detail
    assert world.stub.stop_calls == 0, '被拒绝时不该真的去停服'
    assert world.managed.state == State.RUNNING, '被拒绝时状态不该变'
    assert world.managed.action_in_progress is False, '拒绝后必须把"动作进行中"标志放掉'


def test_untrustworthy_count_is_refused_too(env):
    """事件说没人、查询说有人 → 计数不可信，这时也不许休眠（宁可不关）。"""
    world = env(online_players=3, sample=())
    allowed, why = world.sup._can_sleep(world.managed)
    assert not allowed
    assert '无法确认' in why, why


def test_role_self_trusts_mcdr_events_even_when_the_query_is_down(env):
    """role=self 的玩家数由 MCDR 的进出事件给出，精确可信 —— 状态查询挂了也照样能休眠。"""
    world = env(online_players=0, online=False)
    assert world.sup._can_sleep(world.managed) == (True, '')


def test_auto_sleep_restarts_the_timer_when_refused(env, monkeypatch):
    """自动休眠被拒绝（例如刚好有人进来）时，空闲计时清零重来，免得每轮都重试刷日志。"""
    world = env(online_players=0)
    managed = world.managed
    managed.empty_since = time.monotonic() - 3600
    monkeypatch.setattr(world.sup, 'sleep_server', lambda *_a, **_k: (False, '演示：被拒绝'))
    world.sup._check_idle(managed)
    assert managed.empty_since is None, '被拒绝后应当重新计时'


def test_role_command_server_without_status_query_is_refused(env):
    """role=command 的子服如果状态查询不可用，就无从判断有没有人 —— 同样不放行。"""
    world = env(online_players=0, online=False, role='command')
    allowed, why = world.sup._can_sleep(world.managed)
    assert not allowed and '无法确认' in why, why


# --------------------------------------------------------------------------- #
# ② 聊天前缀颜色统一
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('language', ['zh_cn', 'en_us'])
def test_plugin_prefix_uses_one_colour(language):
    catalog = json.load(open(os.path.join(LANG_DIR, f'{language}.json'), encoding='utf-8'))
    prefixes = {match for value in catalog.values()
                for match in re.findall(r'§.\s*\[Idle Guard\]', value)}
    assert prefixes == {'§b[Idle Guard]'}, f'{language} 的前缀色不一致：{sorted(prefixes)}'
