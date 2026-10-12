"""两条线上反馈的回归测试。

**反馈 1：玩家退了，插件还认为他在线 → `!!ig sleep` 被一直拒绝、空闲计时根本不开始。**

现场（用户自己的 Fabric 26.3 + MCDR 2.16.0，日志原文）：

    [00:33:14] System chat: Pau1am left the game          <- 服务端确实报了"离开"
    [00:33:37] Creative: 拒绝休眠（remote，Pau1am）—— Creative 还有 1 名玩家在线（Pau1am）

根因：MCDR 的 `vanilla_handler` 里 `__player_left_regex = r'(?P<name>[^ ]+) left the game'`
配的是 **fullmatch**，而这条消息被服务端加了 `System chat: ` 前缀 → 匹配失败 →
`on_player_left` 永远不触发（进服那条走的是 `X[ip] logged in with entity id N ...`，
所以只有"离开"会漏）。插件只好一直拿着那个名字当"有人在线"。

两层修法：
1. `Supervisor.on_server_output`：插件自己再把服务端输出里的进/退消息认一遍
   （兼容 `System chat: ` 前缀与经典格式；玩家的聊天行一律跳过）；
2. `Supervisor._drop_stale_players`：万一还有别的格式漏掉，就用状态查询兜底 ——
   但动手条件很严（不能动摇"两个信号矛盾时先不动"这条安全线）：那次查询要成功、
   要**连续** `STALE_PLAYER_SECONDS`（60 秒）以上都报 0 人在线，而且这台服"变空"
   必须发生在"最后一次确认有人"**之后**。下面几条测试就是盯着这些门槛的。

**反馈 2：在别的子服敲 `!!ig sleep <别的服>`，只看到"正在休眠…"，真正的原因回不来。**

根因：远程分支（`_handle_remote_action`）对 sleep 一律先回"已受理"、把执行转后台，
而 `_can_sleep` 的拒绝只留在**对端实例自己的日志**里 → 请求方只能等
`_wait_remote_state` 轮询到超时（`stop_timeout` + 30 秒），玩家最后看到的是"等待超时"。
修法：转后台之前**同步**调用 `precheck_sleep`，把拒绝理由当场答回去。
"""
from __future__ import annotations

import os

import idle_guard.supervisor as supervisor_module
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import PingResult
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port


class Info:
    """MCDR 的 Info 只用到这几个字段。"""

    def __init__(self, content: str, is_user: bool = False, is_player: bool = False) -> None:
        self.content = content
        self.is_user = is_user
        self.is_player = is_player


def build(*, idle_timeout: float = 9999.0):
    config = Config(
        instance_name='Creative',
        language='zh_cn',
        check_interval=9999.0,
        state_tick=0.0,                 # 测试自己驱动，不要后台线程
        idle_timeout=idle_timeout,
        min_uptime=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='t', timeout=1.0),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=free_port(), role='watch'),
            ServerEntry(name='Creative', host='127.0.0.1', port=free_port(), role='self'),
        ],
    )
    return Supervisor(StubMCDR(), config, plugin_version='test')


def empty_ping(**kwargs) -> PingResult:
    base = dict(online=True, players_online=0, players_max=20, sample=[], version_name='Fabric')
    base.update(kwargs)
    return PingResult(**base)


def age_the_evidence(managed, seconds: float = 0.0) -> None:
    """把"最后一次确认有人"和"开始变空"一起往前推，模拟真实的时间流逝。

    两个时刻**同时**往前推，是为了保持它们的先后关系不变 ——
    自愈只认"服务端变空发生在最后一次确认有人之后"这个次序。
    """
    span = seconds or (supervisor_module.STALE_PLAYER_SECONDS + 1.0)
    managed.players_seen_at -= span
    if managed.slp_zero_since is not None:
        managed.slp_zero_since -= span


# --------------------------------------------------------------------------- #
# 1. 自己解析服务端的进/退消息
# --------------------------------------------------------------------------- #

def test_new_style_left_message_is_parsed():
    """`System chat: X left the game`（新版服务端）也要能认出"离开"。"""
    supervisor = build()
    try:
        supervisor.on_player_joined('Pau1am')
        assert supervisor.state_of('Creative').players == {'Pau1am'}

        supervisor.on_server_output(Info('System chat: Pau1am left the game'))

        assert supervisor.state_of('Creative').players == set(), \
            'MCDR 解析不出来的离开消息，插件自己得认出来'
    finally:
        supervisor.shutdown()


def test_classic_left_messages_still_work():
    """经典格式（没有前缀）与 `lost connection` 一样要认。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        for player, line in (('Steve', 'Steve left the game'),
                             ('Alex', 'Alex lost connection: Disconnected')):
            supervisor.on_player_joined(player)
            supervisor.on_server_output(Info(line))
            assert player not in managed.players, line
    finally:
        supervisor.shutdown()


def test_join_message_is_parsed_too():
    supervisor = build()
    try:
        supervisor.on_server_output(Info('System chat: Notch joined the game'))
        assert supervisor.state_of('Creative').players == {'Notch'}
    finally:
        supervisor.shutdown()


def test_player_chat_cannot_fake_a_leave():
    """有人把这句话打进聊天里，不能影响计数（is_user / is_player 的行一律跳过）。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        supervisor.on_server_output(Info('System chat: Pau1am left the game', is_user=True))
        supervisor.on_server_output(Info('<Pau1am> System chat: Pau1am left the game', is_player=True))
        assert managed.players == {'Pau1am'}
    finally:
        supervisor.shutdown()


def test_unrelated_lines_are_ignored():
    supervisor = build()
    try:
        for line in ('Pau1am joined the game and left the game',          # 不是完整一句
                     'System chat: a very long name here left the game',  # 名字非法
                     '[voicechat] Player Pau1am successfully connected to voice chat',
                     ''):
            supervisor.on_server_output(Info(line))
        assert supervisor.state_of('Creative').players == set()
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 2. 用"更新的状态查询"兜住仍然漏掉的离开
# --------------------------------------------------------------------------- #

def test_stale_player_is_dropped_by_a_newer_status_query():
    """服务端连续一分钟以上都说"0 人在线"，而最后一次确认有人更早 —— 那就把人清掉。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        assert supervisor._effective_players(managed)[0] == 1

        supervisor._update_managed(managed, empty_ping())        # 服务端自己说没人
        age_the_evidence(managed)                                # ……而且已经这么说了一分钟

        assert managed.players_seen_at < managed.slp_zero_since
        assert supervisor._effective_players(managed)[0] == 0
        assert managed.players == set()
    finally:
        supervisor.shutdown()


def test_a_single_empty_query_does_not_drop_a_player():
    """刚做出来的**一次**"0 人"结论不能用来踢人（两个信号矛盾时先不动）。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        supervisor._update_managed(managed, empty_ping())

        players, note = supervisor._effective_players(managed)
        assert players == 1, f'一次 0 人结论就把进服事件推翻了（{note}）'
        assert managed.players == {'Pau1am'}
    finally:
        supervisor.shutdown()


def test_a_join_after_the_server_became_empty_blocks_the_self_heal():
    """服务端"变空"之后玩家才进来 → 那个 0 人是旧结论，绝不能拿它清人。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor._update_managed(managed, empty_ping())        # 先有一份"0 人"的结论
        supervisor.on_player_joined('Steve')                     # 之后玩家才进来
        age_the_evidence(managed)

        assert managed.players_seen_at > managed.slp_zero_since
        assert supervisor._effective_players(managed)[0] == 1
        assert managed.players == {'Steve'}
    finally:
        supervisor.shutdown()


def test_old_status_query_never_drops_a_player():
    """查询结论比"确认有人在"更早 → 那是他还没进来时的结果，不能用来把人踢掉。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor._update_managed(managed, empty_ping())        # 先有一份"0 人"的旧结论
        supervisor.on_player_joined('Pau1am')                    # 之后玩家才进来

        assert managed.slp_at < managed.players_seen_at
        players, note = supervisor._effective_players(managed)
        assert players == 1, f'拿旧查询当"人已经走了"是危险的（{note}）'
        assert managed.players == {'Pau1am'}
    finally:
        supervisor.shutdown()


def test_a_broken_query_never_counts_as_empty():
    """查不通（服务端关了 enable-status / 探测失败）不算"连续 0 人"，不能据此清人。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        supervisor._update_managed(managed, empty_ping())
        supervisor._update_managed(managed, PingResult(online=False, error='模拟：连不上'))
        age_the_evidence(managed)

        assert managed.slp_zero_since is None
        assert supervisor._effective_players(managed)[0] == 1
    finally:
        supervisor.shutdown()


def test_query_reporting_players_never_drops_anyone():
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        supervisor._update_managed(managed, empty_ping(players_online=1, sample=['Pau1am']))
        age_the_evidence(managed)
        assert managed.slp_zero_since is None
        assert supervisor._effective_players(managed)[0] == 1
        assert managed.players == {'Pau1am'}
    finally:
        supervisor.shutdown()


def test_zero_run_resets_when_someone_is_seen_again():
    """中间出现过"有人"→ 0 人的连续计数要重来（防止拿断断续续的查询凑够一分钟）。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        supervisor.on_player_joined('Pau1am')
        supervisor._update_managed(managed, empty_ping())
        assert managed.slp_zero_since is not None

        supervisor._update_managed(managed, empty_ping(players_online=1, sample=['Pau1am']))
        assert managed.slp_zero_since is None
    finally:
        supervisor.shutdown()


def test_stale_player_stops_the_idle_timer_only_until_the_query_corrects_it():
    """整条链：人走了（没事件）→ 查询纠正后空闲计时正常开始。"""
    supervisor = build(idle_timeout=1.0)
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.RUNNING
        supervisor.on_player_joined('Pau1am')
        supervisor._update_managed(managed, empty_ping(players_online=1, sample=['Pau1am']))
        supervisor._check_idle(managed)
        assert managed.empty_since is None, '有人在线时不该开始计时'

        supervisor._update_managed(managed, empty_ping())         # 人走了
        age_the_evidence(managed)
        supervisor._check_idle(managed)
        assert managed.empty_since is not None, '人都走了，空闲计时还不开始'
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- #
# 3. 远程休眠：拒绝理由必须当场答回去
# --------------------------------------------------------------------------- #

def test_remote_sleep_reports_the_refusal_immediately():
    """对端有人在线时，远程请求要在总线超时内就回"不能休眠"，而不是先回"已受理"。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.RUNNING
        supervisor.on_player_joined('Pau1am')

        reply = supervisor._handle_remote_action(
            {'action': 'sleep', 'server': 'Creative', 'by': 'Pau1am'})

        assert reply['ok'] is False, '有人在线还给"已受理"，玩家只能干等到超时'
        assert 'Pau1am' in reply['reason'] and '不能休眠' in reply['reason'], reply
    finally:
        supervisor.shutdown()


def test_remote_sleep_is_accepted_when_the_server_is_empty(monkeypatch):
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.RUNNING
        spawned = []

        def fake_background(name, reason, by):
            spawned.append((name, reason, by))
            return True, '已受理'

        monkeypatch.setattr(supervisor, '_sleep_in_background', fake_background)
        reply = supervisor._handle_remote_action(
            {'action': 'sleep', 'server': 'Creative', 'by': 'Pau1am'})

        assert reply['ok'] is True and spawned == [('Creative', 'remote', 'Pau1am')], reply
    finally:
        supervisor.shutdown()


def test_remote_sleep_says_so_when_it_is_already_asleep():
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.SLEEPING

        reply = supervisor._handle_remote_action(
            {'action': 'sleep', 'server': 'Creative', 'by': 'Pau1am'})

        assert reply['ok'] is False and '已休眠' in reply['reason'], reply
    finally:
        supervisor.shutdown()


def test_precheck_sleep_matches_the_real_sleep_path():
    """预检的结论要和真正执行时一致（不一致就会出现"说能睡、结果没睡"）。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.RUNNING
        assert supervisor.precheck_sleep('Creative') == (True, '')

        supervisor.on_player_joined('Pau1am')
        allowed, why_not = supervisor.precheck_sleep('Creative')
        assert allowed is False and '不能休眠' in why_not

        assert supervisor.precheck_sleep('Nope')[0] is False
    finally:
        supervisor.shutdown()


def test_precheck_sleep_agrees_with_sleep_server_when_someone_is_inside():
    """有人在线时：预检说不许，真正的 sleep_server 也不许（文案来自同一处）。"""
    supervisor = build()
    try:
        managed = supervisor.state_of('Creative')
        managed.state = State.RUNNING
        supervisor.on_player_joined('Pau1am')

        _allowed, why_not = supervisor.precheck_sleep('Creative')
        ok, reason = supervisor.sleep_server('Creative', reason='remote', by='Pau1am')

        assert ok is False
        assert reason == why_not
        assert managed.state == State.RUNNING, '拒绝了却把服停了'
    finally:
        supervisor.shutdown()


def test_module_still_has_no_third_party_imports():
    """顺手确认没引入新依赖（插件是纯 Python 零依赖的）—— 拆过模块之后要查整个包。"""
    package_dir = os.path.dirname(supervisor_module.__file__)
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith('.py'):
            continue
        source = open(os.path.join(package_dir, name), encoding='utf-8').read()
        for forbidden in ('import requests', 'import aiohttp', 'import numpy'):
            assert forbidden not in source, f'{name} 里出现了第三方依赖：{forbidden}'
