"""role 判定测试。

回归真实踩过的坑：实例的端口匹配到了自己的服务端，但那个条目的 `role` 被写成了 `watch`，
结果本实例"谁都不管"，用户执行 `!!ig sleep <服>` 只会得到"没有实例负责 xxx"。
"""
from __future__ import annotations

from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.supervisor import Supervisor
from mc_test_utils import StubMCDR, free_port


def build(tmp_path, monkeypatch, own_role: str):
    own_port, other_port = free_port(), free_port()
    # 让插件能从 <cwd>/server/server.properties 读到"我的端口"（模拟真实的 MCDR 实例布局）
    server_dir = tmp_path / 'server'
    server_dir.mkdir(exist_ok=True)
    (server_dir / 'server.properties').write_text(f'server-port={own_port}\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)

    config = Config(
        instance_name='auto',
        check_interval=9999.0,
        idle_timeout=9999.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=[
            ServerEntry(name='Survival', host='127.0.0.1', port=own_port, role=own_role, auto_sleep=False),
            ServerEntry(name='Creative', host='127.0.0.1', port=other_port, role='auto', auto_sleep=True),
        ],
    )
    stub = StubMCDR()
    return Supervisor(stub, config), stub


def test_role_watch_is_not_managed_and_warns(tmp_path, monkeypatch):
    instance, stub = build(tmp_path, monkeypatch, own_role='watch')
    try:
        assert instance.instance_name == 'Survival', '端口匹配应当仍能认出实例名'
        assert instance.state_of('Survival') is None, 'role=watch 时不应该管理它'
        assert instance.state_of('Creative') is None
        log_text = stub.logger.text()
        assert '只会监视它、不会启停它' in log_text, f'缺少明确提示：\n{log_text}'
        assert 'watch' in log_text
    finally:
        instance.shutdown()


def test_role_auto_claims_own_server(tmp_path, monkeypatch):
    instance, stub = build(tmp_path, monkeypatch, own_role='auto')
    try:
        managed = instance.state_of('Survival')
        assert managed is not None and managed.role == 'self', 'role=auto 时应当按端口认领自己的服务端'
        assert instance.state_of('Creative') is None, '别人的子服只监视'
        assert instance.instance_name == 'Survival'
    finally:
        instance.shutdown()


def test_unowned_server_action_explains_reason(tmp_path, monkeypatch):
    instance, stub = build(tmp_path, monkeypatch, own_role='watch')
    try:
        ok, detail = instance.request_action('sleep', 'Creative', by='测试')
        assert not ok
        assert 'role' in detail and 'auto 或 self' in detail, detail
    finally:
        instance.shutdown()
