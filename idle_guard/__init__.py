"""
Idle Guard —— Velocity 群组服子服务器空闲守卫（MCDR 插件）

功能：
1. 定期侦察各子服务器人数（协议层状态查询，无需在子服装任何东西）；
2. 某台由本实例负责的子服连续无人超过阈值（默认 1 小时）后自动关闭；
3. 关闭期间由"假服务端"顶替端口：玩家尝试连接时会收到提示，并自动触发服务端启动；
4. 服务端就绪后再次通知玩家；所有通知都可以通过总线同步到群组内其它子服。

安装：把本目录整体放进 MCDR 的 `plugins/` 下（或使用打包好的 .mcdr），
配置见 `config/idle_guard/config.json`，命令见 `!!ig`。
界面语言由配置里的 `language` 决定（auto = 跟随 MCDR），文案在 `lang/<语言>.json`。
"""
from __future__ import annotations

import os
from typing import Optional

from mcdreforged.api.types import PluginServerInterface

from . import commands, i18n
from .config import Config
from .supervisor import Supervisor

PLUGIN_ID = 'idle_guard'
PLUGIN_VERSION = '1.0.0'

_supervisor: Optional[Supervisor] = None


def _load_config(server: PluginServerInterface) -> Config:
    """读取插件配置。配置损坏时保留原文件并报错，绝不静默重置（避免丢掉整份拓扑配置）。"""
    try:
        return server.load_config_simple('config.json', target_class=Config, failure_policy='raise')
    except Exception as exc:  # noqa: BLE001
        server.logger.error(f'配置文件 config/idle_guard/config.json 读取失败：{type(exc).__name__}: {exc}')
        server.logger.error('原文件已保留，不会被覆盖。请修正后执行 !!MCDR plugin reload idle_guard')
        raise


def on_load(server: PluginServerInterface, prev_module) -> None:
    global _supervisor
    if prev_module is not None:
        previous = getattr(prev_module, '_supervisor', None)
        if previous is not None:
            try:
                previous.shutdown()
            except Exception:  # noqa: BLE001 - 旧实例清理失败不应阻止新实例加载
                server.logger.warning('清理旧插件实例时出现异常', exc_info=True)

    # 语言文件：内置在插件包的 lang/ 下；config/idle_guard/lang/ 里的同名文件可以覆盖它们
    i18n.set_override_dir(os.path.join(server.get_data_folder(), 'lang'))

    config = _load_config(server)
    _supervisor = Supervisor(server, config, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    _supervisor.start()
    commands.register(server, _supervisor)
    # help 列表里的说明交给 MCDR 按它自己的语言挑（不点插件界面的语言）；
    # 权限传 PERM_ADMIN：`!!ig` 全部命令都只给管理员用，所以不该出现在普通玩家的 !!help 里
    server.register_help_message('!!ig', {
        'zh_cn': i18n.translate('command.help.mcdr', 'zh_cn'),
        'en_us': i18n.translate('command.help.mcdr', 'en_us'),
    }, permission=commands.PERM_ADMIN)
    server.logger.info(f'Idle Guard v{PLUGIN_VERSION} 已加载（界面语言 {_supervisor.language}，'
                       f'每 {int(config.check_interval)} 秒侦察一次，'
                       f'无人 {int(config.idle_timeout // 60)} 分钟后休眠）')


def on_unload(server: PluginServerInterface) -> None:
    global _supervisor
    if _supervisor is not None:
        try:
            _supervisor.shutdown()
        finally:
            _supervisor = None


def on_mcdr_stop(server: PluginServerInterface) -> None:
    on_unload(server)


# --------------------------------------------------------------------------- #
# MCDR 事件转发
# --------------------------------------------------------------------------- #

def on_server_startup(server: PluginServerInterface) -> None:
    if _supervisor is not None:
        _supervisor.on_server_startup()


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    if _supervisor is not None:
        _supervisor.on_server_stop(server_return_code)


def on_player_joined(server: PluginServerInterface, player: str, info) -> None:
    if _supervisor is not None:
        _supervisor.on_player_joined(player)


def on_player_left(server: PluginServerInterface, player: str) -> None:
    if _supervisor is not None:
        _supervisor.on_player_left(player)


def on_info(server: PluginServerInterface, info) -> None:
    # 每一条服务端输出都会来这儿：插件自己再认一遍"XX 离开了游戏"，
    # 补 MCDR 解析不出来的格式（例如新版服务端的 `System chat: XX left the game`）。
    if _supervisor is not None:
        _supervisor.on_server_output(info)
