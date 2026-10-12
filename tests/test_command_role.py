"""role=command 的生命周期测试：由 shell 命令启停的子服（不由 MCDR 管理）。

被测对象是 tests/fixtures/dummy_server.py —— 一个真实监听端口、真实应答状态查询的最小服务端；
"启停"完全走 role=command 的 start_command / stop_command 路径，覆盖了命令模式的真实执行流程。
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.mcproto import port_open, status_ping
from idle_guard.supervisor import State, Supervisor
from mc_test_utils import StubMCDR, free_port, login_attempt, wait_until

HERE = os.path.dirname(os.path.abspath(__file__))
DUMMY = os.path.join(HERE, 'fixtures', 'dummy_server.py')
TOUCH = os.path.join(HERE, 'fixtures', 'touch_file.py')


@pytest.fixture
def command_env(tmp_path):
    port = free_port()
    stop_file = str(tmp_path / 'stop.flag')
    python = sys.executable
    config = Config(
        instance_name='controller',
        check_interval=0.5,
        idle_timeout=2.0,
        min_uptime=0.3,
        startup_timeout=25.0,
        stop_timeout=15.0,
        port_release_timeout=10.0,
        wake_cooldown=1.0,
        fallback_startup_seconds=2.0,
        player_attempt_window=60.0,
        debug=True,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=True),
        announce=AnnounceConfig(enabled=True),
        servers=[
            ServerEntry(
                name='third', host='127.0.0.1', port=port, role='command',
                auto_sleep=True, auto_wake=True,
                start_command=f'"{python}" "{DUMMY}" --port {port} --stop-file "{stop_file}"',
                stop_command=f'"{python}" "{TOUCH}" --path "{stop_file}"',
                working_directory=str(tmp_path),
            ),
        ],
    )
    stub = StubMCDR()
    supervisor = Supervisor(stub, config)
    yield SimpleNamespace(sup=supervisor, stub=stub, port=port, stop_file=stop_file)

    try:
        with open(stop_file, 'w', encoding='utf-8'):
            pass
    except OSError:
        pass
    supervisor.shutdown()
    for _ in range(40):
        if not port_open('127.0.0.1', port, 0.2):
            break
        time.sleep(0.2)


def test_command_role_start_stop_cycle(command_env):
    env = command_env
    sup = env.sup
    sup.start()

    # 起始状态：端口没人监听 -> 插件应让假服务端接管（因为 auto_wake=true）
    assert wait_until(lambda: status_ping('127.0.0.1', env.port, 1.0).is_fake, 25), '假服务端没有接管端口'
    assert wait_until(lambda: sup.state_of('third').state == State.SLEEPING, 20)

    # 唤醒：真实执行 start_command
    ok, detail = sup.request_action('wake', 'third', by='测试')
    assert ok, detail
    assert wait_until(lambda: sup.state_of('third').state == State.RUNNING, 30), '命令模式启动失败'
    ping = status_ping('127.0.0.1', env.port, 1.0)
    assert ping.online and not ping.is_fake, '启动后应当是真实服务端在应答'
    assert any('已开启' in text for text in env.stub.said), '没有广播"已开启"'

    # 空闲：真实执行 stop_command
    assert wait_until(lambda: sup.state_of('third').state == State.SLEEPING, 45), '命令模式没有按空闲休眠'
    assert wait_until(lambda: status_ping('127.0.0.1', env.port, 1.0).is_fake, 20), '休眠后假服务端没有接管端口'
    assert any('现已休眠' in text for text in env.stub.said), '没有广播"已休眠"'


def test_command_role_login_triggers_start(command_env):
    env = command_env
    sup = env.sup
    sup.start()
    assert wait_until(lambda: status_ping('127.0.0.1', env.port, 1.0).is_fake, 25)

    # 玩家尝试连接 -> 应当被抱回提示，并触发命令模式启动
    reason = login_attempt('127.0.0.1', env.port, name='Alex')
    assert isinstance(reason, dict) and reason, '假服务端没有返回断开原因'
    assert wait_until(lambda: sup.state_of('third').state == State.RUNNING, 30), '连接尝试没有触发命令模式启动'
    assert any('正在进入' in text for text in env.stub.said), '没有广播"正在启动"'
