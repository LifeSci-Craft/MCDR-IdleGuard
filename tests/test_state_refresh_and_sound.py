"""三处线上反馈的回归测试。

1. 让某台子服休眠后，`!!ig status` 里它还是"运行中"、开关还是 [休眠] ——
   因为**别的实例管着的子服**用的是本地那份"上一次侦察"的旧快照（默认 10 分钟前），
   现在改成整体采用负责方刚算出来的那一份。
2. 对已经休眠的服再 `!!ig sleep`、对已经开着的服再 `!!ig wake`，
   命令层会先把"正在休眠 / 正在启动…"那句先喊出去 —— 现在执行前先侦测状态再回话。
3. 玩家被挡在休眠服外面、服开好之后，除了聊天栏提醒再给他一个音效（音符盒）。
"""
from __future__ import annotations

import time

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor, safe_player_name
from mc_test_utils import StubMCDR, free_port, wait_until


class Source:
    """MCDR 的控制台 / 玩家来源（两种渲染都覆盖一下）。"""

    def __init__(self, console: bool = True, player: str = 'Steve', level: int = 4) -> None:
        self.is_console = console
        self.player = player
        self.level = level
        self.replies: list = []
        self.widgets: list = []

    def reply(self, text) -> None:
        self.widgets.append(text)
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return self.level >= level

    def text(self) -> str:
        return '\n'.join(self.replies)

    def find(self, keyword: str) -> list:
        return [line for line in self.replies if keyword in line]


def build(*, idle_timeout: float = 9999.0, players: int = 0, state: str = State.RUNNING.value):
    """一个单实例环境：本实例只管 Survival，Creative 由"别的实例"管（不真起总线）。"""
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=0.3,
        idle_timeout=idle_timeout,
        min_uptime=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        wake_cooldown=0.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self'),
            # Creative 由"别的实例"管：真实配置里必须写它的总线地址，否则本实例根本联系不上那台实例。
            # （这里原来没写 bus，只是靠"桩忽略入参"才让测试跑过去；现在 `live_views()` 只为
            #  "有地址、且没有可用缓存"的对端发问，所以这一格必须补上才符合真实场景。）
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                        bus='127.0.0.1:25601'),
        ],
    )
    stub = StubMCDR()
    supervisor = Supervisor(stub, config, plugin_version='test')
    view = supervisor._build_view(config.servers[1], None)      # Creative 的"旧快照"
    view.state = state
    view.players = players
    view.players_max = 20
    view.instance = 'Creative'
    supervisor._views['Creative'] = view
    return supervisor, stub, view


# --------------------------------------------------------------------------- #
# 1. 状态表用的是负责方刚算出来的数据
# --------------------------------------------------------------------------- #

def test_peer_view_replaces_the_stale_local_snapshot(monkeypatch):
    """负责方的应答要**整份采用**，不能让本地旧快照把状态盖回去。"""
    supervisor, _stub, stale = build(state=State.RUNNING.value)
    try:
        peer = {
            'name': 'Creative', 'host': '127.0.0.1', 'port': stale.port, 'role': 'self',
            'state': State.SLEEPING.value, 'players': 0, 'players_max': 20,
            'idle_seconds': -1.0, 'idle_timeout': 3600.0, 'locked': False, 'detail': '',
        }
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative', 'servers': [peer]}])
        views, live = supervisor.live_views()
        assert views['Creative'].state == State.SLEEPING.value, '没有采用负责方的状态'
        assert 'Creative' in live
        assert views['Creative'].local is False, '远端的子服不能被标成"本实例"'
        assert views['Creative'].instance == 'Creative'
    finally:
        supervisor.shutdown()


def test_unreachable_peer_keeps_the_cache_but_marks_it_untrusted(monkeypatch):
    """对方不在线时退回旧快照，但要标记成"不可信"——不能拿它去拦管理员的操作。"""
    supervisor, _stub, _stale = build(state=State.RUNNING.value)
    try:
        monkeypatch.setattr(supervisor._bus, 'broadcast', lambda *a, **k: [])
        views, live = supervisor.live_views()
        assert views['Creative'].state == State.RUNNING.value     # 本地还有份缓存
        assert 'Creative' not in live                             # 但不可信
        assert 'Survival' in live                                 # 本实例负责的是可信的
    finally:
        supervisor.shutdown()


def test_switch_button_follows_the_fresh_state(monkeypatch):
    """状态刷新后，最后一列的开关也要跟着变（休眠中 -> 绿色 [开启]）。"""
    supervisor, _stub, _stale = build(state=State.RUNNING.value)
    try:
        peer = {'name': 'Creative', 'role': 'self', 'state': State.SLEEPING.value,
                'players': 0, 'players_max': 20, 'idle_seconds': -1.0, 'idle_timeout': 3600.0}
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative', 'servers': [peer]}])
        view = supervisor.live_views()[0]['Creative']
        cells = cmd_mod.status_cells(view, 2, supervisor.tr, '!!ig', chat=True)
        assert cells[3].text == supervisor.tr('state.sleeping')
        assert cells[-1].text == supervisor.tr('command.status.switch_wake')
        assert cells[-1].click == '!!ig wake 2'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 2. 执行前先侦测状态
# --------------------------------------------------------------------------- #

def test_sleep_on_a_sleeping_server_says_so_and_asks_nothing(monkeypatch):
    supervisor, _stub, view = build(state=State.SLEEPING.value)
    try:
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative',
                                              'servers': [{**view.to_dict(), 'role': 'self'}]}])
        calls = []
        monkeypatch.setattr(supervisor, 'request_action',
                            lambda *a, **k: calls.append(a) or (True, '不应被调用'))
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'sleep', '2')
        assert any('不用再休眠' in line for line in source.replies), source.text()
        assert not calls, '已经在休眠了还去执行了'
        pending, _expired = supervisor.take_pending_action('控制台')
        assert pending is None, '不该登记待确认'
    finally:
        supervisor.shutdown()


def test_wake_on_a_running_server_is_a_friendly_reminder(monkeypatch):
    supervisor, stub, view = build(state=State.RUNNING.value)
    try:
        view.touched = True
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative',
                                              'servers': [{**view.to_dict(), 'role': 'self',
                                                           'state': State.RUNNING.value}]}])
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'wake', '2')
        assert any('已经开着' in line for line in source.replies), source.text()
        assert not any('正在启动' in line for line in source.replies), '不该先说"正在启动"'
        assert stub.start_calls == 0 and supervisor.state_of('Survival').state != State.STARTING
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize('action,state', [('sleep', State.STARTING.value),
                                          ('sleep', State.STOPPING.value),
                                          ('wake', State.STARTING.value),
                                          ('wake', State.STOPPING.value)])
def test_transient_states_are_reported_as_busy(monkeypatch, action, state):
    supervisor, _stub, view = build(state=state)
    try:
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative',
                                              'servers': [{**view.to_dict(), 'role': 'self',
                                                           'state': state}]}])
        source = Source()
        cmd_mod._cmd_action(source, supervisor, action, '2')
        assert any('正在' in line and '再试' in line for line in source.replies), source.text()
    finally:
        supervisor.shutdown()


def test_sleep_is_refused_up_front_when_players_are_online(monkeypatch):
    """有人在线就别让管理员白输一次确认。"""
    supervisor, _stub, view = build(state=State.RUNNING.value, players=3)
    try:
        monkeypatch.setattr(supervisor._bus, 'broadcast',
                            lambda *a, **k: [{'ok': True, 'instance': 'Creative',
                                              'servers': [{**view.to_dict(), 'role': 'self',
                                                           'players': 3}]}])
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'sleep', '2')
        assert any('还有 3 名玩家在线' in line for line in source.replies), source.text()
        assert not any('确认要休眠' in line for line in source.replies), '还是给了确认提示'
    finally:
        supervisor.shutdown()


def test_untrusted_state_does_not_block_the_action(monkeypatch):
    """拿不到可信状态（对方不在线）时不能拦 —— 那时候本地快照可能是旧的。"""
    supervisor, _stub, view = build(state=State.SLEEPING.value)
    try:
        monkeypatch.setattr(supervisor._bus, 'broadcast', lambda *a, **k: [])
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'sleep', '2')
        assert any('确认要休眠' in line for line in source.replies), source.text()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 3. 音效提醒 + 玩家名过滤
# --------------------------------------------------------------------------- #

def test_sound_is_played_for_the_player_who_was_blocked(monkeypatch):
    supervisor, stub, _view = build()
    try:
        supervisor._config.announce.sound = 'minecraft:block.note_block.pling'
        supervisor.on_player_joined('Steve')
        supervisor.play_sound('Steve', supervisor._config.announce.sound)
        assert any('playsound minecraft:block.note_block.pling master Steve' in cmd
                   for cmd in stub.executed), stub.executed
        assert any('execute at Steve run' in cmd for cmd in stub.executed), '没在他自己的位置播'
    finally:
        supervisor.shutdown()


def test_sound_is_skipped_when_that_player_is_not_here(monkeypatch):
    supervisor, stub, _view = build()
    try:
        supervisor.play_sound('Ghost', 'minecraft:block.note_block.pling')
        assert stub.executed == [], '玩家不在本实例的服里也去发命令了'
    finally:
        supervisor.shutdown()


def test_empty_sound_setting_means_silence():
    supervisor, stub, _view = build()
    try:
        supervisor.on_player_joined('Steve')
        supervisor.play_sound('Steve', '')
        assert stub.executed == []
    finally:
        supervisor.shutdown()


def test_wake_success_tells_and_chimes_for_the_attempting_player():
    """玩家被挡回去、服开好之后：既提醒他也放音效（真实链路：休眠 -> 连接 -> 唤醒）。"""
    supervisor, stub, _view = build()
    try:
        supervisor._config.announce.tell_attempt_players = True
        supervisor._config.announce.sound = 'minecraft:block.note_block.bell'
        managed = supervisor.state_of('Survival')
        managed.players.add('Steve')
        managed.record_attempt('Steve', time.monotonic(), supervisor._config.player_attempt_window)
        monkeypatch_free = supervisor._config.startup_timeout

        assert supervisor.wake_server('Survival', reason='connect', by='player',
                                      trigger_player='Steve')[0]
        assert wait_until(lambda: stub.start_calls >= 1, 5)
        stub.started_up = True
        supervisor.on_server_startup()
        assert wait_until(lambda: managed.state == State.RUNNING, 15), managed.state
        assert wait_until(lambda: any('playsound minecraft:block.note_block.bell' in cmd
                                      for cmd in stub.executed), 5), stub.executed
        assert any(player == 'Steve' and '已经开启' in text for player, text in stub.told), stub.told
        assert monkeypatch_free > 0
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize('name,expected', [
    ('Steve', 'Steve'),
    ('player_1', 'player_1'),
    ('bad name', ''),
    ('x' * 17, ''),
    ('Injected] run op @a', ''),
    ('', ''),
    (None, ''),
])
def test_player_names_must_be_legal_before_they_reach_a_command(name, expected):
    assert safe_player_name(name) == expected


def test_bogus_login_name_is_not_remembered_or_used():
    """握手里的名字来自客户端，不能原样进 tellraw / playsound。"""
    supervisor, stub, _view = build()
    try:
        supervisor._on_login_attempt('Survival', 'Evil] run op @a', '127.0.0.1:1')
        managed = supervisor.state_of('Survival')
        assert managed.attempt_players(time.monotonic(), 60) == []
        supervisor.play_sound('Evil] run op @a', 'minecraft:block.note_block.pling')
        assert stub.executed == []
    finally:
        supervisor.shutdown()


def test_sound_message_travels_over_the_bus():
    supervisor, stub, _view = build()
    try:
        supervisor.on_player_joined('Steve')
        reply = supervisor._on_bus_message({'type': 'announce', 'kind': 'sound',
                                            'player': 'Steve',
                                            'sound': 'minecraft:block.note_block.bell'})
        assert reply and reply.get('ok') and reply.get('delivered') is True
        assert any('playsound minecraft:block.note_block.bell' in cmd for cmd in stub.executed)
    finally:
        supervisor.shutdown()
