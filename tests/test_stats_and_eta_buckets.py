"""冷 / 热启动的 ETA 分桶，以及 `!!ig stats` 的累计统计。"""
from __future__ import annotations

import json
import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import stats as stats_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


class Source:
    def __init__(self) -> None:
        self.replies: list = []

    def reply(self, text) -> None:
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True

    def text(self) -> str:
        return '\n'.join(self.replies)


def build(*, hot_window: float = 300.0):
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=9999.0,
        idle_timeout=9999.0,
        min_uptime=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        hot_startup_window=hot_window,
        bus=BusConfig(bind=None, token='t', timeout=0.5),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                             auto_sleep=True, auto_wake=True)],
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
# ① 冷 / 热启动分桶
# --------------------------------------------------------------------------- #

def test_hot_and_cold_samples_are_kept_apart(pinger):
    """热启动（刚停没多久）与冷启动（停了很久）的耗时常差一倍 —— ETA 要分开看。"""
    supervisor, _stub = build()
    try:
        # 热启动样本：停机 30 秒，8 秒起来
        supervisor._remember_startup_seconds('Survival', 8.0, down_seconds=30.0)
        supervisor._remember_startup_seconds('Survival', 8.4, down_seconds=45.0)
        # 冷启动样本：停机 1 小时，30 秒起来
        supervisor._remember_startup_seconds('Survival', 30.0, down_seconds=3600.0)
        supervisor._remember_startup_seconds('Survival', 31.0, down_seconds=7200.0)

        managed = supervisor.state_of('Survival')
        managed.last_stopped_at = time.monotonic() - 10.0        # 现在是"热"情境
        assert supervisor.eta_of('Survival') == 8.2, supervisor.eta_of('Survival')

        managed.last_stopped_at = time.monotonic() - 7200.0      # 换成"冷"情境
        assert supervisor.eta_of('Survival') == 30.5, supervisor.eta_of('Survival')
    finally:
        supervisor.shutdown()


def test_falls_back_to_all_samples_when_a_bucket_is_thin(pinger):
    """对应桶里样本不够（<2）时，退回全体中位数，别拿一个样本硬报。"""
    supervisor, _stub = build()
    try:
        supervisor._remember_startup_seconds('Survival', 9.0, down_seconds=20.0)   # 热：只有 1 个
        supervisor._remember_startup_seconds('Survival', 20.0, down_seconds=3600.0)
        supervisor._remember_startup_seconds('Survival', 22.0, down_seconds=3600.0)

        managed = supervisor.state_of('Survival')
        managed.last_stopped_at = time.monotonic() - 5.0         # 热情境，但热样本只有 1 个

        assert supervisor.eta_of('Survival') == 20.0, supervisor.eta_of('Survival')
    finally:
        supervisor.shutdown()


def test_old_style_samples_still_count(pinger):
    """老文件里是纯耗时（没有停机时长）—— 照样参与全体中位数。"""
    supervisor, _stub = build()
    try:
        supervisor._startup_seconds['Survival'] = [(11.0, None), (13.0, None)]
        supervisor.state_of('Survival').last_stopped_at = time.monotonic() - 5.0
        assert supervisor.eta_of('Survival') == 12.0
    finally:
        supervisor.shutdown()


def test_down_seconds_is_none_before_any_stop(pinger):
    supervisor, _stub = build()
    try:
        managed = supervisor.state_of('Survival')
        assert supervisor._down_seconds(managed) is None, '还没停过就不该瞎猜停机时长'
        managed.last_stopped_at = time.monotonic() - 3.0
        assert 3.0 <= (supervisor._down_seconds(managed) or 0) < 5.0
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 累计统计
# --------------------------------------------------------------------------- #

def test_sleep_and_wake_are_counted_and_settled(tmp_path):
    stats = stats_mod.Stats(str(tmp_path / 'stats.json'))
    stats.note_sleep('survival')
    time.sleep(0.05)
    slept = stats.note_wake('survival')

    since, data = stats.snapshot()
    assert data['survival']['sleep'] == 1
    assert data['survival']['wake'] == 1
    assert data['survival']['sleep_seconds'] >= 0.05
    assert slept >= 0.05
    assert since > 0


def test_stats_survive_a_reload(tmp_path):
    path = str(tmp_path / 'stats.json')
    first = stats_mod.Stats(path)
    first.note_sleep('survival')
    time.sleep(0.05)
    first.note_wake('survival')
    first.note_sleep('creative')

    second = stats_mod.Stats(path)
    _since, data = second.snapshot()

    assert data['survival']['sleep'] == 1
    assert data['survival']['sleep_seconds'] > 0.04
    assert data['creative']['sleep'] == 1
    assert data['creative']['sleep_seconds'] == 0        # 还在睡，没结算


def test_broken_stats_file_is_ignored(tmp_path):
    path = tmp_path / 'stats.json'
    path.write_text('{ 这不是 json', encoding='utf-8')
    stats = stats_mod.Stats(str(path))
    assert stats.snapshot()[1] == {}


def test_ongoing_sleep_is_counted_in_the_snapshot(pinger):
    """正在睡的那一段也要算进去，否则管理员会以为它没休眠过。"""
    supervisor, _stub = build()
    try:
        supervisor._stats.note_sleep('Survival')
        time.sleep(0.05)

        _since, data = supervisor.stats_snapshot()

        assert data['Survival']['sleep_seconds'] >= 0.05
    finally:
        supervisor.shutdown()


def test_duration_formatting():
    assert stats_mod.format_duration(45) == '45s'
    assert stats_mod.format_duration(600) == '10m'
    assert stats_mod.format_duration(8100) == '2h 15m'
    assert stats_mod.format_duration(183600) == '2d 3h'


def test_stats_command_renders_a_table(pinger):
    supervisor, _stub = build()
    try:
        supervisor._stats.note_sleep('Survival')
        time.sleep(0.05)
        supervisor._stats.note_wake('Survival')

        source = Source()
        cmd_mod._cmd_stats(source, supervisor)

        text = source.text()
        assert 'Idle Guard' in text, text
        assert 'Survival' in text, text
        assert '合计' in text, text
    finally:
        supervisor.shutdown()


def test_stats_command_says_so_when_there_is_nothing(pinger):
    supervisor, _stub = build()
    try:
        source = Source()
        cmd_mod._cmd_stats(source, supervisor)
        assert '还没有休眠记录' in source.text(), source.text()
    finally:
        supervisor.shutdown()
