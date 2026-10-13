"""配置默认值与口令生成。

三个"默认值选错了"的坑，都是线上问出来的：

- `bus.bind` 默认曾是空的 —— 于是刚装好的配置**必然**总线不启动，用户得先知道"auto"
  这个约定才可能填对（真踩到过：某台实例的子服一直显示"只监视"，查了两轮）；
- `bus.token` 默认曾是一个写死的公开口令 —— 跨机器部署时它等于没有防线；
- `_peer_addresses()` 曾把「本实例自己负责的那条」也算成对端 —— diag 里因此多出一条
  永远连不上的对端，看着像"那台实例挂了"。
"""
from __future__ import annotations

import os
import time

import pytest

from idle_guard import diag as diag_mod
from idle_guard.config import (CONFIG_FILE_ORDER, CONFIG_LAYOUT, FAKE_SERVER_BIND,
                               LEGACY_BUS_TOKEN, AnnounceConfig, BusConfig, Config,
                               FakeServerConfig, ServerEntry, _flatten_layout, new_bus_token)
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import CollectingLogger, StubMCDR, free_port, wait_until


def build(servers, *, instance='Survival', bind=None, token='a-good-secret', **overrides):
    """造一台实例。bind=None 表示**用默认值**（auto），要显式留空请传 bind=''。"""
    bus_kwargs = {'token': token, 'timeout': 0.6}
    if bind is not None:
        bus_kwargs['bind'] = bind
    settings = {
        'instance_name': instance,
        'language': 'zh_cn',
        'check_interval': 9999.0,
        'state_tick': 9999.0,
        'idle_timeout': 9999.0,
        'min_uptime': 0.0,
        'bus': BusConfig(**bus_kwargs),
        'fake_server': FakeServerConfig(enabled=False),
        'announce': AnnounceConfig(enabled=False),
        'servers': servers,
    }
    settings.update(overrides)
    stub = StubMCDR()
    return Supervisor(stub, Config(**settings), plugin_version='test'), stub


@pytest.fixture
def pinger(monkeypatch):
    """状态查询固定返回"有人开着的空服"，这样心跳就能把状态推到 RUNNING。"""
    def fake_status(host, port, timeout=1.5, **kwargs):
        return PingResult(online=True, players_online=0, players_max=20, sample=[],
                          version_name='Fabric')

    import idle_guard.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, 'status_ping', fake_status)


def owned(bus=None, name='Survival', role='self'):
    """本实例负责的那条子服。"""
    return ServerEntry(name=name, host='127.0.0.1', port=free_port(), role=role,
                       auto_sleep=False, bus=bus)


def stranger(name, bus, *, auto_sleep=True):
    """别人负责（或没人负责）的那条，只用来验证地址选择。"""
    return ServerEntry(name=name, host='127.0.0.1', port=free_port(), role='auto',
                       auto_sleep=auto_sleep, bus=bus)


# --------------------------------------------------------------------------- #
# ① bus.bind 的默认值
# --------------------------------------------------------------------------- #

def test_bind_defaults_to_auto():
    """默认 auto：同一份配置复制到每台机器就能各取各的地址，不必为每台改一个端口。"""
    assert BusConfig().bind == 'auto'


def test_auto_takes_the_address_from_the_entry_this_instance_owns():
    supervisor, _stub = build([owned(bus='127.0.0.1:25599')])
    try:
        assert supervisor.bus_bind == '127.0.0.1:25599'
        assert supervisor.bus_bind_state() == ('ok', '')
    finally:
        supervisor.shutdown()


def test_auto_without_an_address_names_the_entry_and_warns():
    """解析不到时要说清是**哪一条**缺地址 —— 这是那次"只监视"排查的起点。"""
    supervisor, _stub = build([owned(bus=None)])
    try:
        assert supervisor.bus_bind_state() == ('auto_no_bus', 'Survival')
        joined = '\n'.join(supervisor.config_warnings())
        assert 'Survival' in joined, joined
        assert 'bus' in joined, joined
    finally:
        supervisor.shutdown()


def test_a_watch_only_instance_is_not_told_off():
    """本实例不负责任何子服时用不上总线 —— 这是正常情况，不该在启动时报警。"""
    supervisor, stub = build([stranger('Survival', '127.0.0.1:25599')], instance='Watcher')
    try:
        assert supervisor.bus_bind_state() == ('auto_nothing', '')
        assert supervisor.config_warnings() == []
        assert '总线不会启动' not in stub.logger.text(), stub.logger.text()
    finally:
        supervisor.shutdown()


def test_command_only_instance_can_still_resolve_auto():
    """全靠 role=command 的实例没有 role=self 条目，auto 也该能从"我负责的那条"取到地址。"""
    supervisor, _stub = build([
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='command',
                    auto_sleep=True, bus='127.0.0.1:25601', start_command='echo start',
                    stop_command='echo stop', working_directory=os.getcwd()),
    ], instance='Controller')
    try:
        assert supervisor.bus_bind == '127.0.0.1:25601'
        assert supervisor.bus_bind_state() == ('ok', '')
    finally:
        supervisor.shutdown()


def test_an_empty_bind_is_treated_as_auto():
    """留空（老配置里默认写的就是空）按 auto 处理 —— 老配置文件升级后自动获得正确行为。"""
    supervisor, _stub = build([owned(bus='127.0.0.1:25599')], bind='')
    try:
        assert supervisor.bus_bind == '127.0.0.1:25599'
        assert supervisor.bus_bind_state() == ('ok', '')
    finally:
        supervisor.shutdown()


def test_a_malformed_bind_does_not_start_the_bus():
    supervisor, stub = build([owned(bus='127.0.0.1:25599')], bind='off')
    try:
        assert supervisor.bus_bind_state() == ('invalid', 'off')
        assert supervisor.bus_bind is None
        assert '不是有效地址' in stub.logger.text(), stub.logger.text()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② bus.token 的生成
# --------------------------------------------------------------------------- #

def test_token_default_is_a_fresh_random_string():
    first, second = BusConfig().token, BusConfig().token
    assert first != second, '两个实例不该共用同一个口令'
    assert first != LEGACY_BUS_TOKEN
    assert len(first) >= 16


def test_new_bus_token_is_url_safe_and_unique():
    values = {new_bus_token() for _ in range(50)}
    assert len(values) == 50, '生成的随机串出现了重复'
    assert all(character.isalnum() or character in '-_' for value in values
               for character in value), '口令里出现了不便复制粘贴的字符'


def test_a_config_that_lacks_the_token_still_gets_a_random_one():
    """手改过 / 老版本的配置里缺 token 时，不能退化成空口令或公开口令。"""
    config = Config.deserialize({'instance_name': 'X'})
    assert config.bus.token and config.bus.token != LEGACY_BUS_TOKEN
    assert config.bus.bind == 'auto', '缺 bind 时也该拿到 auto 这个默认值'


def test_an_existing_token_is_never_replaced():
    config = Config.deserialize({'bus': {'bind': '127.0.0.1:1', 'token': 'mine'}})
    assert config.bus.token == 'mine'
    assert config.bus.bind == '127.0.0.1:1'


def test_legacy_token_is_still_flagged():
    supervisor, _stub = build([owned(bus='127.0.0.1:25599')], token=LEGACY_BUS_TOKEN)
    try:
        joined = '\n'.join(supervisor.config_warnings())
        assert 'token' in joined, joined
    finally:
        supervisor.shutdown()


def test_token_fingerprint_is_short_stable_and_not_the_secret():
    supervisor, _stub = build([owned(bus='127.0.0.1:25599')], token='top-secret-token')
    try:
        fingerprint = supervisor.bus_token_fingerprint()
        assert len(fingerprint) == 8
        assert fingerprint == supervisor.bus_token_fingerprint()
        assert 'top-secret-token' not in fingerprint
    finally:
        supervisor.shutdown()


def test_check_rows_show_the_fingerprint_so_two_instances_can_be_compared():
    supervisor, _stub = build([owned(bus='127.0.0.1:25599')])
    try:
        text = '\n'.join(line for line, _colour in diag_mod.check_rows(supervisor))
        assert supervisor.bus_token_fingerprint() in text, text
    finally:
        supervisor.shutdown()


class _FakePluginServer:
    """只实现 `_load_config` 用到的那几个接口。"""

    def __init__(self, folder, config) -> None:
        self._folder = folder
        self._config = config
        self.logger = CollectingLogger()
        self.loaded: list = []

    def get_data_folder(self) -> str:
        return self._folder

    def load_config_simple(self, name, **kwargs):
        self.loaded.append(name)
        return self._config


def test_first_run_prints_the_token_fingerprint_not_the_token(tmp_path):
    """首次生成配置时只印**指纹**，不印口令本身。

    口令必须与其它实例一致，所以要提醒一句；但口令不该进日志 ——
    这个插件的日志习惯是"出问题就整段贴出来"，而 8 位指纹足够两边对照。
    """
    from idle_guard import _load_config
    from idle_guard.supervisor import token_fingerprint

    config = Config(instance_name='Survival')
    fake = _FakePluginServer(str(tmp_path), config)
    assert _load_config(fake) is config
    log = fake.logger.text()
    assert token_fingerprint(config.bus.token) in log, log
    assert config.bus.token not in log, f'口令本身不该出现在日志里：{log}'
    assert '填到' in log or '复制' in log, log


def test_reload_does_not_repeat_the_first_run_notice(tmp_path):
    (tmp_path / 'config.json').write_text('{}', encoding='utf-8')
    from idle_guard import _load_config

    fake = _FakePluginServer(str(tmp_path), Config(instance_name='Survival'))
    _load_config(fake)
    assert '已生成新配置' not in fake.logger.text()


# --------------------------------------------------------------------------- #
# ③ 对端列表里不许出现"自己"
# --------------------------------------------------------------------------- #

def test_our_own_entry_is_not_treated_as_a_peer():
    """回归：插件曾把本实例自己那条也当对端去连，于是 diag 里多出一条永远连不上的对端。

    最容易撞上的写法：`bus.bind` 填了一个地址、而"本实例负责那条"的 `bus` 又填了另一个
    （两边看着都像对的）。
    """
    supervisor, _stub = build([
        owned(bus='127.0.0.1:25600'),                # 本实例负责那条
        stranger('Creative', '127.0.0.1:25601'),
    ], bind='127.0.0.1:25599')                       # 本实例的监听地址与那条不同
    try:
        assert supervisor.bus_bind == '127.0.0.1:25599'
        assert supervisor._peer_addresses() == ['127.0.0.1:25601']
    finally:
        supervisor.shutdown()


def test_our_listen_address_is_not_a_peer_either():
    """另外两条填了和本实例相同的地址时，也不该去连它。"""
    supervisor, _stub = build([
        owned(bus=None),
        stranger('Mirror', '127.0.0.1:25599', auto_sleep=False),   # 与本实例同址（配置写重了）
        stranger('Creative', '127.0.0.1:25601'),
    ], bind='127.0.0.1:25599')
    try:
        assert supervisor._peer_addresses() == ['127.0.0.1:25601']
    finally:
        supervisor.shutdown()


def test_watch_only_bus_line_in_diag_is_gray_not_yellow():
    """诊断页里"用不上总线"应该是灰色提示，不是黄色的"出问题了"。"""
    supervisor, _stub = build([stranger('Survival', '127.0.0.1:25599')], instance='Watcher')
    try:
        row = next((item for item in diag_mod.rows(supervisor) if '总线未启动' in item[0]), None)
        assert row is not None, [text for text, _colour in diag_mod.rows(supervisor)]
        assert row[1] == diag_mod.RColor.gray
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ④ 文件里的"按功能分组"布局（只改长相，字段一个都不能丢）
# --------------------------------------------------------------------------- #

def test_the_grouped_file_keeps_every_field():
    """分组只是**文件长相**：漏掉一个字段就等于"配置里改了没反应"。"""
    grouped = Config.get_default().serialize()
    assert set(grouped) == set(CONFIG_FILE_ORDER), '文件的段落与顺序应对得上'
    assert set(_flatten_layout(grouped)) == set(Config.get_field_annotations())


def test_the_layout_only_mentions_real_fields():
    for group, names in CONFIG_LAYOUT.items():
        for name in names:
            assert name in Config.get_field_annotations(), f'{group} 里的 {name} 不是配置字段'


def test_an_old_flat_config_is_still_read():
    """老配置是平铺写法 —— 升级后必须照样生效，而不是静默回到默认值。"""
    config = Config.deserialize({'idle_timeout': 7200, 'min_uptime': 60, 'check_interval': 120,
                                 'debug': True, 'bus': {'token': 'keep'}})
    assert (config.idle_timeout, config.min_uptime, config.check_interval) == (7200, 60, 120)
    assert config.debug is True and config.bus.token == 'keep'
    assert config.serialize()['sleep']['idle_timeout'] == 7200, '再写出来就是分组的样子'


def test_a_grouped_config_is_read_too():
    config = Config.deserialize({'sleep': {'idle_timeout': 1800, 'state_tick': 0},
                                 'startup': {'startup_timeout': 30}})
    assert (config.idle_timeout, config.state_tick, config.startup_timeout) == (1800, 0, 30)


def test_the_flat_value_wins_when_both_places_exist():
    """两处都写时以顶层为准（它更显眼，用户改的通常是那一处）。"""
    assert Config.deserialize({'idle_timeout': 111, 'sleep': {'idle_timeout': 222}}).idle_timeout == 111


def test_announce_keeps_its_own_fields_beside_the_layout_group():
    """`announce` 本身就是个对象：往里塞布局字段不能把它自己的字段挤掉。"""
    grouped = Config.get_default().serialize()
    assert set(grouped['announce']) == {'enabled', 'tell_attempt_players', 'sound',
                                        'player_attempt_window'}
    config = Config.deserialize(grouped)
    assert config.announce.enabled is True
    assert config.player_attempt_window == 600.0


def test_removed_fields_stay_removed():
    """这几个字段是刻意删掉的（见 CHANGELOG），别在重构里又加回来。"""
    assert 'grace_after_wake' not in set(Config.get_field_annotations())       # 并进了 min_uptime
    assert 'warn_before_sleep' not in set(AnnounceConfig.get_field_annotations())
    assert 'bind' not in set(FakeServerConfig.get_field_annotations())         # 写死 0.0.0.0
    assert 'startup_timeout' not in set(ServerEntry.get_field_annotations())   # 只留全局
    assert FAKE_SERVER_BIND == '0.0.0.0'


# --------------------------------------------------------------------------- #
# ⑤ 启动保护期（现在它同时承担"唤醒后别立刻关"）
# --------------------------------------------------------------------------- #

def test_min_uptime_protects_a_freshly_ready_server(pinger):
    supervisor, stub = build([
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    auto_sleep=True, auto_wake=True),
    ], idle_timeout=0.2, min_uptime=30.0, check_interval=0.2)
    try:
        supervisor.start()
        assert wait_until(lambda: supervisor.state_of('Survival').state == State.RUNNING, 10)
        time.sleep(1.0)                    # 早就过了 0.2 秒的空闲阈值
        assert supervisor.state_of('Survival').state == State.RUNNING, '保护期内不该休眠'
        assert stub.stop_calls == 0
    finally:
        supervisor.shutdown()
