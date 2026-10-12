"""配置自检：把"启动时只在日志里说一次"的配置问题摊开。

为什么要它：线上真踩过 —— `bus.bind: auto` 取不到值让整个跨实例协作失效，表现却是
「某个子服一直是只监视」，前后排查了两轮。插件其实一直知道（日志里有那条 warning），
只是埋在两三百行里说了一次。自检把它变成"启动就报 + 随时可查"。
"""
from __future__ import annotations

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import diag as diag_mod
from idle_guard.config import (AnnounceConfig, BusConfig, Config, FakeServerConfig,
                               ServerEntry)
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


def build(servers=None, *, bus_bind='auto', token='a-good-secret'):
    """默认造一台「本实例负责 Survival」的实例；servers 可整套换掉。"""
    if servers is None:
        servers = [
            # 注意：故意**不写** bus —— `bus.bind: auto` 就是靠它取值的
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True, auto_wake=True),
        ]
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=9999.0,                 # 心跳关掉：测试里手动驱动，避免抢跑
        idle_timeout=9999.0,
        min_uptime=0.0,
        bus=BusConfig(bind=bus_bind, token=token, timeout=0.6),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=servers,
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
# ① 总线相关的两条
# --------------------------------------------------------------------------- #

def test_auto_bind_that_cannot_resolve_names_the_entry():
    """`bus.bind: auto` 取不到值时，要说清是**哪一条**少写了 `bus`。

    这是线上那个"某个子服一直只监视"的真正起点 —— 一句抽象的"总线未配置"没法排查。
    """
    supervisor, _stub = build(bus_bind='auto')          # Survival 那条没写 bus
    try:
        problems = supervisor.config_warnings()
        joined = '\n'.join(problems)
        assert 'Survival' in joined, problems
        assert 'auto' in joined and 'bus' in joined, problems
    finally:
        supervisor.shutdown()


def test_empty_bind_falls_back_to_auto_instead_of_shouting():
    """`bus.bind` 留空（老配置的默认写法就是空）不再是一种"状态" —— 按 auto 处理。

    否则每个老配置升级后都得先手改一格才会好，等于把"默认值选错"的账转嫁给用户。
    """
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25600'),
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='auto',
                    auto_sleep=True, bus='127.0.0.1:25601'),
    ], bus_bind='')
    try:
        assert supervisor.bus_bind == '127.0.0.1:25600', '留空该照抄"自己那条"的地址'
        assert supervisor.config_warnings() == []
    finally:
        supervisor.shutdown()


def test_a_malformed_bind_is_reported_with_the_value():
    """填错格式（例如写了个 `off`）要说出来，而不是让它静默失效到 Bus.start() 才报错。"""
    supervisor, _stub = build(bus_bind='off')
    try:
        assert supervisor.bus_bind_state() == ('invalid', 'off')
        joined = '\n'.join(supervisor.config_warnings())
        assert 'off' in joined and 'bus.bind' in joined, joined
        assert supervisor.bus_bind is None
    finally:
        supervisor.shutdown()


def test_a_working_bus_produces_no_complaint_about_bus():
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=True, bus='127.0.0.1:25599'),
    ], bus_bind='auto')
    try:
        assert supervisor.bus_bind == '127.0.0.1:25599'
        assert supervisor.config_warnings() == []
    finally:
        supervisor.shutdown()


def test_the_same_bus_address_twice_is_called_out():
    """两条填同一个地址 → 它们会互相抢端口。"""
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25599'),
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='auto',
                    auto_sleep=True, bus='127.0.0.1:25599'),
    ], bus_bind='auto')
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'Survival' in joined and 'Creative' in joined, joined
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 没人管的子服
# --------------------------------------------------------------------------- #

def test_watch_role_with_auto_sleep_is_contradictory():
    """role=watch（只监视）却开着自动休眠 —— 没有任何实例会去关它。"""
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25599'),
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                    auto_sleep=True),
    ], bus_bind='auto')
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'Creative' in joined, joined
    finally:
        supervisor.shutdown()


def test_auto_role_without_bus_is_called_out():
    """role=auto 期望"端口与它一致的那台 MCDR"来管，可没写 bus 就等于没人知道去哪找它。"""
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25599'),
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='auto',
                    auto_sleep=True),                      # 没写 bus
    ], bus_bind='auto')
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'Creative' in joined, joined
    finally:
        supervisor.shutdown()


def test_always_on_servers_are_not_complained_about():
    """常驻子服本来就"不该被关"，不该出现在自检里。"""
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=True, always_on=True, bus='127.0.0.1:25599'),
    ], bus_bind='auto')
    try:
        assert supervisor.config_warnings() == []
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ③ 默认口令
# --------------------------------------------------------------------------- #

def test_default_bus_token_is_flagged():
    supervisor, _stub = build(bus_bind=None, token='idle_guard')
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'token' in joined, joined
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ④ 接进启动流程与命令
# --------------------------------------------------------------------------- #

def test_start_reports_the_problems_to_the_console(pinger):
    """启动时就该喊出来，而不是等用户去敲 diag。"""
    supervisor, stub = build(bus_bind='auto')
    try:
        supervisor.start()
        log = stub.logger.text()
        assert '配置自检' in log, log[-600:]
        assert 'Survival' in log, log[-600:]
    finally:
        supervisor.shutdown()


def test_check_command_renders_the_self_check(pinger):
    supervisor, _stub = build(bus_bind='auto')
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)

        source = Source()
        cmd_mod._cmd_check(source, supervisor)
        text = source.text()
        assert '配置自检' in text, text
        assert 'Survival' in text, text
    finally:
        supervisor.shutdown()


def test_check_rows_report_all_clear_when_the_config_is_fine(pinger):
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=True, bus='127.0.0.1:25599'),
    ], bus_bind='auto')
    try:
        text = '\n'.join(line for line, _colour in diag_mod.check_rows(supervisor))
        assert '没有发现问题' in text, text
    finally:
        supervisor.shutdown()


def test_check_is_registered_with_help():
    """新命令要出现在帮助里，并且能裸跑。"""
    assert ('check', True) in cmd_mod._HELP_ROWS


# --------------------------------------------------------------------------- #
# ⑤ 两条子服同名
# --------------------------------------------------------------------------- #

def test_duplicated_server_names_are_called_out():
    """同一个名字出现两次：`_managed` 以名字为键，后一条会静默覆盖前一条，

    而状态表按配置顺序渲染会列出两行同名 —— 管理员以为配了两台，实际只有一台受管。
    """
    supervisor, _stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False),
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False),
    ], bus_bind='auto')
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'Survival' in joined and '2' in joined, joined
    finally:
        supervisor.shutdown()


def test_a_duplicated_name_is_warned_about_at_startup(pinger):
    supervisor, stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25600'),
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=False, bus='127.0.0.1:25600'),
    ], bus_bind='auto')
    try:
        supervisor.start()
        log = stub.logger.text()
        assert '都叫 Survival' in log, log[-600:]
    finally:
        supervisor.shutdown()
