"""命令层测试。

重点覆盖三个真实踩过的坑：

1. MCDR 的 ConsoleCommandSource **没有 `player` 属性**，命令回调里直接写 `source.player`
   会在控制台执行时抛 AttributeError；
2. 命令回调跑在 MCDR 自己的任务线程上，**回调里绝不能阻塞** —— 之前 `!!ig sleep`
   同步等到服务端停稳 + 端口接管，MCDR 直接报 "TaskExecutor 线程已经 10 秒没有响应了"；
3. `!!ig reload` 要"先回正在重载，再回重载完成"。
"""
from __future__ import annotations

import threading
import time

import pytest

from idle_guard import commands as cmd_mod
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.supervisor import Supervisor
from mc_test_utils import StubMCDR, free_port, wait_until


class ConsoleLikeSource:
    """模仿 MCDR 的 ConsoleCommandSource：有 reply / has_permission，但没有 player 属性。

    reply 收到的可能是 RText（界面用），这里按 MCDR 的做法转成纯文本再断言。
    """

    def __init__(self) -> None:
        self.replies: list = []
        self.lock = threading.Lock()

    def reply(self, text) -> None:
        plain = text.to_plain_text() if hasattr(text, 'to_plain_text') else str(text)
        with self.lock:
            self.replies.append(plain)

    def has_permission(self, level: int) -> bool:
        return True

    @property
    def is_console(self) -> bool:
        return True

    def find(self, keyword: str) -> list:
        with self.lock:
            return [line for line in self.replies if keyword in line]


class PlayerLikeSource(ConsoleLikeSource):
    player = 'Steve'

    @property
    def is_console(self) -> bool:
        return False

    def reply(self, text) -> None:
        self.widgets = getattr(self, 'widgets', [])
        self.widgets.append(text)
        super().reply(text)


@pytest.fixture
def supervisor():
    config = Config(
        instance_name='survival',
        language='zh_cn',
        check_interval=60.0,
        idle_timeout=9999.0,
        min_uptime=0.0,
        startup_timeout=2.0,
        stop_timeout=2.0,
        port_release_timeout=2.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False),
        announce=AnnounceConfig(enabled=False),
        servers=[ServerEntry(name='survival', host='127.0.0.1', port=free_port(), role='self')],
    )
    instance = Supervisor(StubMCDR(), config)
    yield instance
    instance.shutdown()


def test_help_works_from_console_like_source(supervisor):
    source = ConsoleLikeSource()
    cmd_mod._line_help(source, supervisor, '!!ig')
    assert any('Idle Guard' in line for line in source.replies)


def test_status_from_console_like_source(supervisor):
    source = ConsoleLikeSource()
    cmd_mod._cmd_status(source, supervisor)
    assert any('Idle Guard' in line for line in source.replies)
    assert any('survival' in line for line in source.replies)
    assert any('开关' in line for line in source.replies)


def test_status_always_lists_every_server_without_arguments(supervisor):
    """`!!ig status` 不带参数：一次列出全部（用户要求去掉"只看一个"的用法）。"""
    source = ConsoleLikeSource()
    cmd_mod._cmd_status(source, supervisor)
    rows = [line for line in source.replies if line.startswith('1  ')]
    assert rows, f'状态表里没有编号列：{source.replies}'


def test_lock_action_is_immediate(supervisor):
    """锁类操作不涉及等待，应当同步回结果。"""
    source = ConsoleLikeSource()
    cmd_mod._cmd_action(source, supervisor, 'lock', 'survival')
    assert source.replies and '锁定' in source.replies[0]
    assert supervisor.state_of('survival').locked is True


def test_action_from_player_like_source(supervisor):
    source = PlayerLikeSource()
    cmd_mod._cmd_action(source, supervisor, 'lock', 'survival')
    assert source.replies
    assert supervisor.state_of('survival').locked is True


def test_action_on_unknown_server_reports_error(supervisor):
    source = ConsoleLikeSource()
    cmd_mod._cmd_action(source, supervisor, 'wake', 'nope')
    assert any('没有编号或名字是 nope' in line for line in source.replies)


def test_action_does_not_block_and_reports_result(supervisor, monkeypatch):
    """回归：唤醒必须异步执行，且完成后补一条结果。"""
    seen = {}

    def slow_request(name_action, server, by='console', wait=False):
        seen['wait'] = wait
        time.sleep(1.0)          # 模仿真实的"等服务端停稳/启动"
        return True, f'演示结果：{name_action} {server}'

    monkeypatch.setattr(supervisor, 'request_action', slow_request)

    source = ConsoleLikeSource()
    started = time.monotonic()
    cmd_mod._cmd_action(source, supervisor, 'wake', 'survival')
    cost = time.monotonic() - started

    assert cost < 0.3, f'命令回调阻塞了 {cost:.2f} 秒，MCDR 会判定为卡死'
    assert source.replies, '应当先回一条"正在执行"'
    assert wait_until(lambda: bool(source.find('演示结果')), 5.0), '缺少完成后的结果回复'
    assert seen.get('wait') is True, '后台执行时应当要求等到最终结果'


def test_sleep_waits_for_confirmation_before_doing_anything(supervisor, monkeypatch):
    """回归（用户要求）：`!!ig sleep` 只登记待确认，`!!ig confirm` 才真的关服。"""
    seen = {}

    def slow_request(action, server, by='console', wait=False):
        seen['wait'] = wait
        time.sleep(0.5)
        return True, f'演示结果：{action} {server}'

    monkeypatch.setattr(supervisor, 'request_action', slow_request)

    source = ConsoleLikeSource()
    started = time.monotonic()
    cmd_mod._cmd_action(source, supervisor, 'sleep', 'survival')
    assert time.monotonic() - started < 0.3
    assert source.find('确认要休眠'), f'没有要求确认：{source.replies}'
    assert not seen, '还没确认就不该开始执行'

    started = time.monotonic()
    cmd_mod._cmd_confirm(source, supervisor)
    assert time.monotonic() - started < 0.3, 'confirm 回调也不该阻塞'
    assert wait_until(lambda: bool(source.find('演示结果')), 5.0)
    assert seen.get('wait') is True


def test_confirm_without_pending_request_says_so(supervisor):
    source = ConsoleLikeSource()
    cmd_mod._cmd_confirm(source, supervisor)
    assert any('没有等你确认' in line for line in source.replies)


def test_poll_command_runs(supervisor):
    source = ConsoleLikeSource()
    cmd_mod._cmd_poll(source, supervisor)
    assert wait_until(lambda: bool(source.find('侦察完成')), 5.0)


def test_reload_command_reports_running_then_done(supervisor):
    """回归：重载要"先提示正在重载，完成后再提示重载完成"。"""
    source = ConsoleLikeSource()
    cmd_mod._cmd_reload(source, supervisor)
    assert wait_until(lambda: bool(source.find('重载完成')), 5.0), f'没有重载完成提示：{source.replies}'
    assert '正在重载' in source.replies[0]
    assert supervisor._mcdr.reloaded == ['idle_guard']


def test_reload_failure_reports_manual_command(supervisor):
    source = ConsoleLikeSource()

    def boom(_plugin_id):
        raise RuntimeError('模拟重载失败')

    supervisor._mcdr.reload_plugin = boom
    cmd_mod._cmd_reload(source, supervisor)
    assert wait_until(lambda: bool(source.find('自动重载失败')), 5.0)
    assert source.find('!!MCDR plugin reload idle_guard')
