"""代码审查（1.1.0 → 1.1.1）修复项的回归测试。

按审查条目分组，每条都对应一个真实会出问题的场景：

1. 忙碌保护在「没有 role=self」的部署里被早返回整段跳过（备份会被自动休眠打断）。
2. 唤醒冷却的时间戳在动作**发起前**就打点：启动失败后玩家重连会被误报「正在启动中」。
3. 接管线程在 bind 之前必须再看一眼状态（`_can_start_fake` 可能花掉半秒）。
4. 插件卸载过程中不许再把端口抢回来。
5. 探测线程池要常驻（以前每个心跳周期新建一个）。
6. 统计文件要原子写、坏文件要留证。
7. 协议层：出站长度校验、VarInt 的 32 位边界。
8. 对端快照的枚举值要降级，不能原样进状态表。
9. 跨实例动作要并行广播，不再逐个串行发送。
10. 重写 config.json 时不能丢掉用户自己加的键。
11. 中英语言文件的占位符集合要一致。
12. `role=command` 的启停命令要回收子进程并报告失败。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time

import pytest

import idle_guard.supervisor as supervisor_module
from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import (
    MAX_PACKET_LENGTH,
    PingResult,
    decode_varint,
    encode_varint,
    pack_packet,
)
from idle_guard.models import ServerView, State
from idle_guard.stats import Stats
from idle_guard.supervisor import Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until

PEER_ADDRESS = '127.0.0.1:25601'


class FakeInfo:
    """服务端输出的一行（`Supervisor.on_server_output` 只用到这几个属性）。"""

    def __init__(self, content: str, *, is_user: bool = False, is_player: bool = False) -> None:
        self.content = content
        self.is_user = is_user
        self.is_player = is_player


class Source:
    """MCDR 的控制台来源（够命令层用）。"""

    def __init__(self, console: bool = True) -> None:
        self.is_console = console
        self.replies: list = []

    def reply(self, text) -> None:
        self.replies.append(text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text))

    def has_permission(self, level: int) -> bool:
        return True

    def text(self) -> str:
        return '\n'.join(self.replies)


def build(entries=None, *, tmp_path=None, fake_server: bool = False,
          wake_cooldown: float = 0.0) -> tuple:
    """一个最小可用的 Supervisor（默认：本实例只负责 Survival）。"""
    servers = list(entries) if entries is not None else [
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    bus='127.0.0.1:25600'),
    ]
    config = Config(
        instance_name='Survival',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=9999.0,
        idle_timeout=9999.0,
        min_uptime=0.0,
        wake_cooldown=wake_cooldown,
        bus=BusConfig(bind='127.0.0.1:25600', token='t', timeout=0.5),
        fake_server=FakeServerConfig(enabled=fake_server),
        announce=AnnounceConfig(enabled=False),
        servers=servers,
    )
    stub = StubMCDR()
    if tmp_path is not None:
        stub.data_folder = str(tmp_path)
    return Supervisor(stub, config, plugin_version='test'), stub


def two_servers() -> list:
    """本实例管 Survival；Creative 由另一台实例管（有总线地址，可远程操作）。"""
    return [
        ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='self',
                    bus='127.0.0.1:25600'),
        ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='watch',
                    bus=PEER_ADDRESS),
    ]


# --------------------------------------------------------------------------- #
# ① 忙碌保护：不再被「没有 role=self」的早返回跳过

def test_backup_hold_applies_even_without_any_role_self_server():
    supervisor, _stub = build([ServerEntry(
        name='Creative', host='127.0.0.1', port=free_port(), role='command',
        start_command='echo start', stop_command='echo stop', auto_sleep=True,
    )])
    try:
        managed = supervisor._managed['Creative']
        supervisor.on_server_output(FakeInfo('[Backup] Backing up world...'))
        assert managed.busy_until > time.monotonic(), '纯 role=command 部署里忙碌保护失效了'
        assert 'Backing up' in managed.busy_reason
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ② 唤醒冷却的时间戳

def test_a_failed_wake_clears_the_cooldown_so_players_can_retry(monkeypatch):
    supervisor, _stub = build(wake_cooldown=15.0)
    try:
        managed = supervisor._managed['Survival']
        managed.state = State.OFFLINE
        monkeypatch.setattr(supervisor, '_do_wake', lambda *a, **k: None)

        managed.last_wake_ts = time.monotonic()
        _ok, detail = supervisor.wake_server('Survival', reason='connect', by='player')
        assert detail == supervisor.tr('action.wake_cooldown', server='Survival')

        supervisor._wake_failed(managed, 'detail.wake_start_failed', error='模拟失败')
        assert managed.last_wake_ts == 0.0, '失败之后还留着冷却时间戳'

        _ok, detail = supervisor.wake_server('Survival', reason='connect', by='player')
        assert detail != supervisor.tr('action.wake_cooldown', server='Survival'), \
            '启动失败之后玩家重连被误报成「正在启动中」'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ③④ 端口接管

def test_takeover_rechecks_the_state_right_before_binding(monkeypatch):
    """`_can_start_fake` 可能花掉最长 0.5 秒（TCP 探测），这期间唤醒可能已经开始。

    没有 bind 前的那次复查，就可能把一台"正在启动的真实服务端"的端口抢过来。
    """
    supervisor, _stub = build(fake_server=True)
    try:
        managed = supervisor._managed['Survival']
        managed.state = State.SLEEPING
        starts: list = []

        def can_start(inner):
            inner.state = State.STARTING          # 模拟：探测期间唤醒把状态推进了
            return True

        monkeypatch.setattr(supervisor, '_can_start_fake', can_start)
        monkeypatch.setattr('idle_guard.fakeserver.FakeServer.start',
                            lambda self, force=False: starts.append(force) or True)
        supervisor._ensure_fake_server(managed)
        assert starts == [], '状态已经变成 STARTING，不能再抢端口'
    finally:
        supervisor.shutdown()


def test_port_is_not_taken_back_while_the_plugin_is_shutting_down():
    supervisor, _stub = build(fake_server=True)
    try:
        managed = supervisor._managed['Survival']
        managed.state = State.SLEEPING
        supervisor._stopping.set()
        supervisor._ensure_fake_server(managed)
        assert managed.fake is None or not managed.fake.running
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ⑤ 常驻探测线程池

def test_the_probe_pool_is_created_once_not_per_tick(monkeypatch):
    created: list = []
    real = supervisor_module.concurrent.futures.ThreadPoolExecutor

    def counting(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(supervisor_module.concurrent.futures, 'ThreadPoolExecutor', counting)
    supervisor, _stub = build()
    try:
        assert len(created) == 1, '构造时就该建好那一个池'
        monkeypatch.setattr(supervisor_module, 'status_ping', lambda *a, **k: PingResult())
        supervisor._tick_states()
        supervisor._ping_all()
        assert len(created) == 1, f'心跳/侦察又新建了 {len(created) - 1} 个线程池'
    finally:
        supervisor.shutdown()


def test_shutdown_closes_the_probe_pool():
    supervisor, _stub = build()
    supervisor.shutdown()
    with pytest.raises(RuntimeError):
        supervisor._pool.submit(lambda: None)


# --------------------------------------------------------------------------- #
# ⑥ 统计文件

def test_stats_are_written_atomically_and_read_back(tmp_path):
    path = str(tmp_path / 'stats.json')
    stats = Stats(path)
    stats.note_sleep('survival')
    stats.note_wake('survival')
    assert os.path.isfile(path)
    assert not os.path.exists(path + '.tmp'), '临时文件应当已经被 os.replace 换走了'

    again = Stats(path)
    _since, servers = again.snapshot()
    assert servers['survival']['sleep'] == 1
    assert servers['survival']['wake'] == 1


def test_a_corrupt_stats_file_is_kept_aside_instead_of_being_overwritten(tmp_path):
    path = tmp_path / 'stats.json'
    path.write_text('{ 这不是 json', encoding='utf-8')
    stats = Stats(str(path))
    assert stats.snapshot()[1] == {}
    assert (tmp_path / 'stats.json.corrupt').exists(), '坏文件应当改名留证'


# --------------------------------------------------------------------------- #
# ⑦ 协议层边界

@pytest.mark.parametrize('raw', [
    b'\xff\xff\xff\xff\xff\x01',      # 6 字节：协议里不存在
    b'\xff\xff\xff\xff\x7f',          # 第 5 字节的数据位超出 32 位
])
def test_decode_varint_rejects_values_wider_than_32_bits(raw):
    with pytest.raises(ValueError):
        decode_varint(raw)


def test_decode_varint_still_accepts_the_full_32_bit_range():
    assert decode_varint(b'\xff\xff\xff\xff\x0f')[0] == 0xFFFFFFFF
    assert decode_varint(encode_varint(300))[0] == 300
    assert decode_varint(encode_varint(-1))[0] == 0xFFFFFFFF


def test_pack_packet_refuses_an_oversized_payload():
    pack_packet(0x00, b'x' * 16)                     # 正常大小不该有事
    with pytest.raises(ValueError):
        pack_packet(0x00, b'x' * (MAX_PACKET_LENGTH + 1))


# --------------------------------------------------------------------------- #
# ⑧ 对端快照的枚举值

def test_unknown_role_and_state_from_a_peer_are_downgraded():
    view = ServerView.from_dict({'name': 'Creative', 'role': 'superuser', 'state': 'exploded'})
    assert view.role == 'watch'
    assert view.state == State.UNKNOWN.value

    ok = ServerView.from_dict({'name': 'Creative', 'role': 'command',
                               'state': State.RUNNING.value})
    assert (ok.role, ok.state) == ('command', State.RUNNING.value)


# --------------------------------------------------------------------------- #
# ⑨ 跨实例动作并行广播

def test_remote_action_broadcasts_instead_of_sending_one_by_one():
    supervisor, _stub = build(two_servers())
    try:
        calls: list = []
        supervisor._bus.broadcast = lambda addresses, message, timeout=None: (
            calls.append((list(addresses), message)) or [{'ok': True, 'reason': '受理了'}])
        supervisor._bus.send = lambda *a, **k: pytest.fail('不该再逐个串行发送')
        ok, detail = supervisor.request_action('sleep', 'Creative')
        assert ok is True and detail == '受理了', (ok, detail)
        assert calls and calls[0][0] == [PEER_ADDRESS], calls
        assert calls[0][1]['server'] == 'Creative'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ⑩ 重写配置时不丢未知键

def test_rewriting_the_config_keeps_keys_the_plugin_does_not_know(tmp_path):
    supervisor, _stub = build(tmp_path=tmp_path)
    try:
        path = tmp_path / 'config.json'
        path.write_text(json.dumps({
            'servers': [],
            'my_custom_key': 42,
            'sleep': {'idle_timeout': 60, 'user_note': '别删我'},
        }), encoding='utf-8')
        supervisor._save_config()
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['my_custom_key'] == 42, '用户自己加的顶层键被丢掉了'
        assert saved['sleep']['user_note'] == '别删我', '分组里的未知键被丢掉了'
        assert saved['servers'], '插件自己的字段还是要写回去'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ⑪ 语言文件

def test_language_files_keep_identical_placeholders():
    root = pathlib.Path(__file__).resolve().parent.parent / 'idle_guard' / 'lang'
    zh = json.loads((root / 'zh_cn.json').read_text(encoding='utf-8'))
    en = json.loads((root / 'en_us.json').read_text(encoding='utf-8'))
    pattern = re.compile(r'\{(\w+)\}')
    mismatch = {key: (sorted(pattern.findall(zh[key])), sorted(pattern.findall(en[key])))
                for key in zh
                if key in en and sorted(pattern.findall(zh[key])) != sorted(pattern.findall(en[key]))}
    assert mismatch == {}, mismatch


def test_the_offline_note_names_the_server_in_both_languages():
    """回归：英文那份曾经少了 {server}，而"键一致"的测试抓不到这种不对称。"""
    root = pathlib.Path(__file__).resolve().parent.parent / 'idle_guard' / 'lang'
    for name in ('zh_cn.json', 'en_us.json'):
        data = json.loads((root / name).read_text(encoding='utf-8'))
        assert '{server}' in data['hover.note.offline'], name


# --------------------------------------------------------------------------- #
# ⑬ 读路径不联网之后：远端缓存只用于展示，不能用来拦操作

def test_a_stale_peer_cache_must_not_block_a_remote_wake():
    """端到端实测踩到的回归。

    场景：survival 刚休眠 1 秒，另一个实例上敲 `!!ig wake survival`。
    那时心跳缓存里还留着"运行中"的快照（下一次心跳才会更新），旧实现（读路径同步探测）
    会拿到新鲜状态；改成只读缓存之后，这份"一秒前的真相"被当成了拒绝依据 ——
    于是回一句"已经开着了"，唤醒请求根本没发出去。

    契约：远端的缓存只用于**展示**；要拦操作，得由负责那台服的实例用自己的权威状态来拦。
    """
    supervisor, _stub = build(two_servers())
    try:
        peer = {'name': 'Creative', 'role': 'self', 'state': State.RUNNING.value,
                'players': 0, 'players_max': 20, 'idle_seconds': -1.0, 'idle_timeout': 3600.0}
        supervisor._peer_snapshots[PEER_ADDRESS] = {
            'at': time.monotonic(), 'instance': 'Creative',
            'servers': [peer], 'fresh_probe': True,
        }
        sent: list = []
        supervisor.request_action = (lambda action, server, by='console', wait=False:
                                     (sent.append((action, server)), (True, '已受理'))[1])
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'wake', 'Creative')
        assert wait_until(lambda: sent == [('wake', 'Creative')], 5), \
            f'远端唤醒请求被旧缓存拦掉了：{source.text()}'
        assert '已经开着' not in source.text()
    finally:
        supervisor.shutdown()


def test_a_local_server_is_still_prechecked_from_its_authoritative_state():
    """本地负责的子服不受影响：状态就是我们自己维护的，该拦还是要拦。"""
    supervisor, _stub = build()
    try:
        supervisor._managed['Survival'].state = State.RUNNING
        sent: list = []
        supervisor.request_action = lambda *a, **k: sent.append(a) or (True, '不该被调用')
        source = Source()
        cmd_mod._cmd_action(source, supervisor, 'wake', 'Survival')
        assert any('已经开着' in line for line in source.replies), source.text()
        assert sent == [], '已经开着的本地服不该再去执行唤醒'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# ⑫ role=command 的启停命令

def test_a_command_that_fails_immediately_is_reported():
    """命令进程要被回收，失败要报出来 —— 以前 Popen 的返回值直接丢掉。"""
    supervisor, stub = build([ServerEntry(
        name='Creative', host='127.0.0.1', port=free_port(), role='command',
        start_command='exit 3', stop_command='exit 0',
    )])
    try:
        supervisor._run_command('exit 3', None, 'internal.what_start_command', 'Creative')
        assert wait_until(lambda: '返回码 3' in stub.logger.text(), 8), stub.logger.text()
    finally:
        supervisor.shutdown()
