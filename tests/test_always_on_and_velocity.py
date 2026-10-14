"""两个新功能：常驻子服（`always_on`）与从 `velocity.toml` 同步子服清单。

- **常驻**：给"希望长期开着、不必休眠"的子服一个总开关。开了之后自动判定不关它，
  管理员手敲 `!!ig sleep`（包括从别的实例发来的）也会被直接拒绝。
- **Velocity 同步**：Velocity 里已经写了每个子服的名字与地址，插件启动时读一遍：
  缺的补进来；默认配置里"一个字没改"的示例条目（lobby / survival）按它改写或删除；
  用户改过的条目一律不动，地址对不上只提醒。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard import velocity as velocity_mod
from idle_guard.config import (AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry,
                               default_servers)
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import ServerView, State, Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


# --------------------------------------------------------------------------- #
# ① 常驻子服
# --------------------------------------------------------------------------- #

def build(*, always_on: bool):
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=0.3,
        idle_timeout=0.2,               # 故意设很短：一到点就该看出来它关不关
        min_uptime=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=True),
        announce=AnnounceConfig(enabled=True),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                        auto_sleep=True, auto_wake=True, always_on=always_on),
        ],
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


def test_always_on_is_never_auto_slept(pinger):
    supervisor, stub = build(always_on=True)
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)

        time.sleep(1.0)                      # 空闲阈值只有 0.2 秒，早就该到点了

        assert managed.state == State.RUNNING, f'常驻子服被自动休眠了（{managed.state.value}）'
        assert stub.stop_calls == 0, '插件不该去停一台常驻子服'
    finally:
        supervisor.shutdown()


def test_always_on_refuses_a_manual_sleep(pinger):
    supervisor, stub = build(always_on=True)
    try:
        supervisor.start()
        managed = supervisor.state_of('Survival')
        assert wait_until(lambda: managed.state == State.RUNNING, 10)

        ok, detail = supervisor.request_action('sleep', 'Survival', by='测试')

        assert not ok, '常驻子服不该被休眠'
        assert '常驻' in detail, detail
        assert stub.stop_calls == 0
    finally:
        supervisor.shutdown()


def test_always_on_is_refused_before_asking_for_confirmation(pinger):
    """命令层的预检就要拦下来：省得管理员输了 `!!ig confirm` 才发现关不了。"""
    supervisor, _stub = build(always_on=True)
    try:
        supervisor.start()
        view = ServerView(name='Survival', host='127.0.0.1', port=1, role='self',
                          state=State.RUNNING.value, players=0, always_on=True)
        message = cmd_mod._precheck_message('sleep', State.RUNNING.value, view, supervisor.tr, 'Survival')
        assert message is not None, '常驻子服上 sleep 应当被预检拦下'
        assert '常驻' in message[0], message
    finally:
        supervisor.shutdown()


def test_always_on_server_gets_no_sleep_button(pinger):
    supervisor, _stub = build(always_on=True)
    try:
        view = ServerView(name='Survival', host='127.0.0.1', port=1, role='self',
                          state=State.RUNNING.value, players=0, always_on=True)
        cell = cmd_mod._switch_cell(view, 1, supervisor.tr, '!!ig', chat=False)
        assert cell.text == supervisor.tr('command.status.always_on'), cell.text
        assert cell.click is None, '常驻子服的开关不该可以点'
    finally:
        supervisor.shutdown()


def test_always_on_is_explained_in_the_status_tooltip(pinger):
    supervisor, _stub = build(always_on=True)
    try:
        view = ServerView(name='Survival', host='127.0.0.1', port=1, role='self',
                          state=State.RUNNING.value, players=0, always_on=True,
                          idle_seconds=600.0, idle_timeout=3600.0)
        lines = cmd_mod._idle_hover_lines(view, supervisor.tr)
        assert any('常驻' in line for line in lines), lines
    finally:
        supervisor.shutdown()


def test_always_on_survives_the_view_roundtrip(pinger):
    """跨实例传的是序列化后的视图：常驻标记不能在路上丢掉。"""
    view = ServerView(name='Survival', host='127.0.0.1', port=1, role='self',
                      state=State.RUNNING.value, always_on=True)
    restored = ServerView.from_dict(view.to_dict(), instance='Survival')
    assert restored.always_on is True


# --------------------------------------------------------------------------- #
# ② 从 velocity.toml 同步
# --------------------------------------------------------------------------- #

TOML = '''\
# 这是 Velocity 的配置
bind = "0.0.0.0:20000"

[servers]
# 每个 key 是一个子服名
Survival = "127.0.0.1:30000"
Creative = "127.0.0.1:30001"     # 行尾注释
try = [
    "Survival"
]

[forced-hosts]
"mc.example.com" = ["Survival"]
'''


def test_parse_servers_skips_everything_that_is_not_a_server(tmp_path):
    path = tmp_path / 'velocity.toml'
    path.write_text(TOML, encoding='utf-8')
    assert velocity_mod.parse_servers(str(path)) == {
        'Survival': ('127.0.0.1', 30000),
        'Creative': ('127.0.0.1', 30001),
    }


def test_parse_servers_is_quiet_when_the_file_is_unreadable(tmp_path):
    assert velocity_mod.parse_servers(str(tmp_path / '没有这个文件.toml')) == {}


def test_auto_mode_looks_next_to_the_mcdr_working_directory(tmp_path):
    velocity_dir = tmp_path / 'Velocity'
    velocity_dir.mkdir()
    (velocity_dir / 'velocity.toml').write_text(TOML, encoding='utf-8')
    assert velocity_mod.find_config(str(tmp_path)) == str(velocity_dir / 'velocity.toml')


def _supervisor_in(tmp_path, monkeypatch, *, servers, velocity_config='auto', toml=TOML):
    (tmp_path / 'server').mkdir(exist_ok=True)
    (tmp_path / 'server' / 'server.properties').write_text('server-port=30000\n', encoding='utf-8')
    (tmp_path / 'velocity.toml').write_text(toml, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    config = Config(
        language='zh_cn',
        check_interval=9999.0,
        bus=BusConfig(bind=None, token='t'),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        velocity_config=velocity_config,
        servers=servers,
    )
    stub = StubMCDR()
    stub.data_folder = str(tmp_path)
    return Supervisor(stub, config, plugin_version='test'), stub


def test_missing_servers_are_added_from_velocity(tmp_path, monkeypatch):
    supervisor, stub = _supervisor_in(tmp_path, monkeypatch, servers=[])
    try:
        names = {entry.name for entry in supervisor.config.servers}
        assert names == {'Survival', 'Creative'}, names
        assert 'try' not in names, '`try` 不是子服'
        added = next(e for e in supervisor.config.servers if e.name == 'Creative')
        assert (added.host, added.port) == ('127.0.0.1', 30001)
        assert '补进' in stub.logger.text(), stub.logger.text()
        # 也要落盘，否则管理员在 config.json 里看不到这几条
        saved = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
        assert {e['name'] for e in saved['servers']} == {'Survival', 'Creative'}
    finally:
        supervisor.shutdown()


def test_existing_entries_are_never_touched(tmp_path, monkeypatch):
    """已经手写过的条目（role / auto_sleep / bus 这些）不能被自动同步改掉。"""
    mine = ServerEntry(name='Survival', host='10.0.0.9', port=12345, role='command',
                       auto_sleep=False, start_command='start.bat')
    supervisor, stub = _supervisor_in(tmp_path, monkeypatch, servers=[mine])
    try:
        entry = next(e for e in supervisor.config.servers if e.name == 'Survival')
        assert (entry.host, entry.port) == ('10.0.0.9', 12345), '手写的地址被覆盖了'
        assert entry.role == 'command' and entry.auto_sleep is False
        assert entry.start_command == 'start.bat'
        assert '保留你的配置' in stub.logger.text(), '地址不一致时要提醒一句'
        # 只补了 Creative，Survival 原样
        assert {e.name for e in supervisor.config.servers} == {'Survival', 'Creative'}
    finally:
        supervisor.shutdown()


def test_sync_can_be_switched_off(tmp_path, monkeypatch):
    supervisor, _stub = _supervisor_in(tmp_path, monkeypatch, servers=[], velocity_config='off')
    try:
        assert supervisor.config.servers == []
        assert not os.path.isfile(os.path.join(str(tmp_path), 'config.json')), '关掉后不该写配置'
    finally:
        supervisor.shutdown()


def test_sync_is_quiet_when_there_is_no_velocity_file(tmp_path, monkeypatch):
    (tmp_path / 'server').mkdir()
    (tmp_path / 'server' / 'server.properties').write_text('server-port=30000\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    config = Config(language='zh_cn', check_interval=9999.0, bus=BusConfig(bind=None),
                    fake_server=FakeServerConfig(enabled=False), announce=AnnounceConfig(enabled=False),
                    servers=[])
    stub = StubMCDR()
    supervisor = Supervisor(stub, config, plugin_version='test')
    try:
        assert supervisor.config.servers == []
    finally:
        supervisor.shutdown()


def test_pristine_samples_are_replaced_by_velocity(tmp_path, monkeypatch):
    """默认配置自带的 lobby / survival 只是示例，不该和真实清单混在一起。"""
    supervisor, stub = _supervisor_in(tmp_path, monkeypatch, servers=default_servers())
    try:
        names = {entry.name for entry in supervisor.config.servers}
        assert names == {'Survival', 'Creative'}, names
        assert '已删除默认配置里的示例条目' in stub.logger.text(), stub.logger.text()
        saved = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
        assert {e['name'] for e in saved['servers']} == {'Survival', 'Creative'}
    finally:
        supervisor.shutdown()


def test_a_sample_matching_velocity_is_rebuilt(tmp_path, monkeypatch):
    """示例条目名与 velocity 里的名字一致时，整条按 velocity 重建（示例里的假 bus 要清掉）。"""
    toml = '[servers]\nsurvival = "127.0.0.1:30000"\n'
    sample = next(e for e in default_servers() if e.name == 'survival')
    supervisor, stub = _supervisor_in(tmp_path, monkeypatch, servers=[sample], toml=toml)
    try:
        entry = next(e for e in supervisor.config.servers if e.name == 'survival')
        assert (entry.host, entry.port) == ('127.0.0.1', 30000), '示例的假端口没被 velocity 改写'
        assert entry.bus is None, '示例里的假总线地址要清掉，否则看起来像已经配好了'
        assert entry.role == 'auto' and entry.auto_sleep is True
        assert '已按 velocity.toml 改写示例条目' in stub.logger.text(), stub.logger.text()
        assert 'bus 为空' in stub.logger.text(), '要提醒管理员填 bus'
    finally:
        supervisor.shutdown()


def test_edited_entries_and_extra_servers_survive_the_cleanup(tmp_path, monkeypatch):
    """用户改过的条目、以及 velocity.toml 里没有的自建条目，都不能被删掉。"""
    edited = ServerEntry(name='survival', host='10.0.0.9', port=12345, role='command',
                         start_command='start.bat', bus='127.0.0.1:25601')
    mine = ServerEntry(name='Mirror', host='127.0.0.1', port=30002, bus='127.0.0.1:25602')
    sample = next(e for e in default_servers() if e.name == 'lobby')
    supervisor, stub = _supervisor_in(tmp_path, monkeypatch, servers=[edited, mine, sample])
    try:
        by_name = {e.name: e for e in supervisor.config.servers}
        assert set(by_name) == {'survival', 'Mirror', 'Survival', 'Creative'}, set(by_name)
        assert (by_name['survival'].host, by_name['survival'].port) == ('10.0.0.9', 12345)
        assert by_name['survival'].start_command == 'start.bat'
        assert (by_name['Mirror'].host, by_name['Mirror'].port) == ('127.0.0.1', 30002)
        assert 'lobby' not in by_name, '没改过的示例条目应该被删掉'
    finally:
        supervisor.shutdown()
