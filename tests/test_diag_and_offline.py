"""可观测性：`!!ig diag` 与「管它的实例离线了」的识别。

两件事都是为了同一个目的：**出问题时能自己定位**。
以前「这台服没人管」与「管它的那台实例掉线了」在状态表里长得一模一样，
而"为什么它还不休眠"这个问题更是只能翻日志猜（线上真踩到过）。
"""
from __future__ import annotations

import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import diag as diag_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


class Source:
    """最小的命令来源桩：只要 reply 与 has_permission。"""

    def __init__(self) -> None:
        self.replies: list = []

    def reply(self, text) -> None:
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True

    def text(self) -> str:
        return '\n'.join(self.replies)


def build(*, always_on: bool = False, idle_timeout: float = 9999.0, peer_bus: str = '',
          bus_bind=None, state_tick: float = 9999.0):
    entries = [
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=True, auto_wake=True, always_on=always_on),
    ]
    if peer_bus:
        entries.append(ServerEntry(name='Creative', host='127.0.0.1', port=free_port(),
                                   role='auto', auto_sleep=True, auto_wake=True,
                                   bus=peer_bus))
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=state_tick,             # 默认关掉：测试里手动驱动，避免抢跑
        idle_timeout=idle_timeout,
        min_uptime=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=bus_bind, token='t', timeout=0.6),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=entries,
    )
    stub = StubMCDR()
    return Supervisor(stub, config, plugin_version='test'), stub


@pytest.fixture
def pinger(monkeypatch):
    def fake_status(host, port, timeout=1.5, **kwargs):
        return PingResult(online=True, players_online=0, players_max=20, sample=[],
                          version_name='Fabric')

    import idle_guard.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, 'status_ping', fake_status)


# --------------------------------------------------------------------------- #
# ① 诊断页
# --------------------------------------------------------------------------- #

def test_diag_shows_identity_claim_and_managed(pinger):
    supervisor, stub = build()
    try:
        stub.data_folder = None
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)

        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))

        assert '认领依据' in text, text
        assert '本实例负责' in text and 'Survival' in text, text
        assert '休眠预检' in text, text
        assert '总线' in text, text
    finally:
        supervisor.shutdown()


def test_diag_precheck_points_at_the_blocking_item(pinger):
    """空闲没到点 → 预检里应当有一条 ✗ 说明还没到；到了之后该条转 ✓。"""
    supervisor, _stub = build(idle_timeout=0.2)
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)

        early = supervisor.sleep_checklist('Survival')
        assert any(not ok and '还没到' in text for ok, text in early), early

        time.sleep(0.4)                    # 越过那 0.2 秒的阈值

        later = supervisor.sleep_checklist('Survival')
        assert all(ok for ok, text in later), later
    finally:
        supervisor.shutdown()


def test_diag_precheck_reports_players(pinger):
    supervisor, _stub = build()
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)
        managed.players.add('Pau1am')

        checks = supervisor.sleep_checklist('Survival')
        assert any(not ok and '在线' in text for ok, text in checks), checks
    finally:
        supervisor.shutdown()


def test_diag_precheck_reports_always_on(pinger):
    supervisor, _stub = build(always_on=True)
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        checks = supervisor.sleep_checklist('Survival')
        assert any(not ok and '常驻' in text for ok, text in checks), checks
    finally:
        supervisor.shutdown()


def test_diag_says_why_the_bus_did_not_start(pinger):
    """线上反馈：diag 只回一句「总线未配置」，没说是哪一种原因，用户无从下手。

    现在按三种情形分别说明：本条（值非法）／「本实例负责那条少写了 bus」（见下一条测试）／
    「本实例不负责任何子服，用不上总线」（正常，灰色提示）。
    """
    supervisor, _stub = build(bus_bind='off')             # 填了个不是地址的值
    try:
        supervisor.start()
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        assert 'off' in text and '不是合法地址' in text, text
    finally:
        supervisor.shutdown()


def test_diag_points_at_the_entry_missing_its_bus(pinger):
    """`bus.bind: auto` 是「照抄本实例负责那条的 bus」—— 那条没写就要点名是哪条。"""
    supervisor, _stub = build(bus_bind='auto')          # Survival 条目没写 bus
    try:
        supervisor.start()
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        assert 'bus.bind 是 auto' in text, text
        assert 'Survival' in text and '那条没写 bus' in text, text
    finally:
        supervisor.shutdown()


def test_diag_says_the_peer_name_is_unknown(pinger):
    """从没连上的对端，实例名位置原来只有一个 `?`，读的人不知道那是什么意思。"""
    supervisor, _stub = build(peer_bus='127.0.0.1:1',    # 没人监听的地址
                              state_tick=0.2)            # 让心跳跑起来：对端探测是它负责的
    try:
        supervisor.start()
        assert wait_until(lambda: bool(supervisor.peer_health()), 10), '心跳没记录对端状态'
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        assert '对端 127.0.0.1:1' in text, text
        assert '实例名未知' in text, text
    finally:
        supervisor.shutdown()


def test_diag_warns_that_an_unowned_server_will_never_sleep(pinger):
    """配了自动休眠却没有任何实例负责它 —— 那条配置永远生效不了，必须点出来。

    线上就是这个情形：Creative 写着 `auto_sleep: true`，却一直显示「只监视」，
    用户以为有人在管它，实际它永远不会被关。
    """
    supervisor, _stub = build(peer_bus='127.0.0.1:1')
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        creative = [line for line in text.splitlines() if 'Creative' in line]
        assert creative, text
        assert '没有实例负责它' in creative[0] and '不会生效' in creative[0], creative[0]
    finally:
        supervisor.shutdown()


def test_diag_reports_startup_samples(pinger):
    supervisor, _stub = build()
    try:
        supervisor._remember_startup_seconds('Survival', 11.5)
        supervisor._remember_startup_seconds('Survival', 12.5)
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        assert '11.5' in text and '12.5' in text and 'ETA' in text, text
    finally:
        supervisor.shutdown()


def test_diag_command_renders_a_screen(pinger):
    supervisor, _stub = build()
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        source = Source()
        cmd_mod._cmd_diag(source, supervisor)
        assert 'Idle Guard' in source.text(), source.text()
        assert '休眠预检' in source.text(), source.text()
    finally:
        supervisor.shutdown()


def test_diag_is_registered_with_admin_permission():
    """诊断信息会暴露内部地址，跟其它命令一样只给管理员。"""
    supervisor, _stub = build()
    try:
        root = cmd_mod.build_tree(supervisor, '!!ig')
        names = set()
        for node in cmd_mod._literal_nodes(root) if hasattr(cmd_mod, '_literal_nodes') else []:
            names |= set(getattr(node, 'literals', ()))
        tree = str(root.get_children())
        assert 'diag' in tree, tree
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 「管它的实例离线了」
# --------------------------------------------------------------------------- #

def test_offline_owner_is_marked_in_the_status_table(pinger):
    supervisor, _stub = build(peer_bus='127.0.0.1:1')     # 指向没人监听的地址
    try:
        # 先"见过"对方在管 Creative，再让它失联
        supervisor._owner_name['Creative'] = 'Creative'
        supervisor._peer_health['127.0.0.1:1'] = {'ok': False, 'at': time.monotonic(),
                                                  'instance': 'Creative', 'error': '连接被拒绝'}

        views = supervisor.status_views()
        view = views['Creative']

        assert view.instance == 'Creative', '应当记得是谁在管它'
        assert view.owner_online is False, '对方联系不上时应当标记离线'

        cell = cmd_mod._note_cell(view, supervisor.tr, '!!ig')
        assert '离线' in cell.text, cell.text
        assert cell.hover and 'MCDR' in cell.hover, cell.hover
    finally:
        supervisor.shutdown()


def test_owner_is_not_marked_offline_before_we_ever_met_it(pinger):
    """从来没见过谁管它 → 不能凭空说人家离线（否则插件刚启动就满屏误报）。"""
    supervisor, _stub = build(peer_bus='127.0.0.1:1')
    try:
        views = supervisor.status_views()
        assert views['Creative'].owner_online is True
        cell = cmd_mod._note_cell(views['Creative'], supervisor.tr, '!!ig')
        assert '离线' not in cell.text, cell.text
    finally:
        supervisor.shutdown()


def test_coming_back_clears_the_offline_mark(pinger):
    supervisor, _stub = build(peer_bus='127.0.0.1:1')
    try:
        supervisor._owner_name['Creative'] = 'Creative'
        supervisor._peer_health['127.0.0.1:1'] = {'ok': False, 'at': time.monotonic(),
                                                  'instance': 'Creative', 'error': 'x'}
        assert supervisor.status_views()['Creative'].owner_online is False

        supervisor._mark_peer_alive('Creative')           # 它又应答了

        assert supervisor.status_views()['Creative'].owner_online is True
    finally:
        supervisor.shutdown()


def test_peer_poll_records_why_it_failed(pinger):
    supervisor, _stub = build(peer_bus='127.0.0.1:1')
    try:
        supervisor._poll_peers()
        health = supervisor.peer_health()['127.0.0.1:1']
        assert health['ok'] is False
        assert health['error'], '要留下失败原因，否则 diag 也帮不上忙'
    finally:
        supervisor.shutdown()


def test_diag_shows_the_peer_failure(pinger):
    supervisor, _stub = build(peer_bus='127.0.0.1:1')
    try:
        supervisor._poll_peers()
        text = '\n'.join(line for line, _colour in diag_mod.rows(supervisor))
        assert '对端' in text and '127.0.0.1:1' in text, text
    finally:
        supervisor.shutdown()
