# CHANGELOG

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本文件只保留最新一个版本的条目**，更早版本的记录见
[GitHub Releases](https://github.com/LifeSci-Craft/MCDR-IdleGuard/releases)。

---

## v1.2.0

### 变更

- 远端子服的 `sleep` / `wake`：由负责该子服的实例判断状态，本实例不再按心跳缓存回绝请求
- 远端子服的 `wake` 不再先回复「正在启动…」，第一句回复即负责方的结论
- 日志不再打印总线口令，只打印 8 位指纹；`!!ig check` 显示指纹
- 假服务端连接数超限时记录一次连接尝试并写一条 warning，然后关闭连接
- 假服务端新增 16 个溢出名额与每来源 IP 配额
- 唤醒冷却仅在启动失败的重试期间生效
- `!!ig status` 的子服编号与命令参数解析保持一致

### 修复

- `bus.bind` / `servers[].bus` 端口超出 1..65535 时按非法地址处理，不再抛 `OverflowError` 导致插件加载失败
- `!!ig status` / `!!ig diag` / `!!ig check` 不再向对端发起网络请求，只使用心跳快照
- 统计文件改为原子写入；无法解析的旧文件改名为 `stats.json.corrupt`
- `role: command` 的子服此前不参与「服务端正在忙」的判断，现已修复
- `role: command` 的启停命令会回收子进程并记录非零退出码
- 对端快照中未知的 `role` / `state` 降级为 `watch` / `unknown`
- 同一子服被两个实例认领时只采用第一个认领者
- `decode_varint` 限制为最多 5 字节 / 32 位；`pack_packet` 增加长度校验
- 语言文件：英文 `state.offline` 改为 `not running`，`hover.note.offline` 补充 `{server}`，删除一个未使用的键

### 内部整理

- 总线：连接总数与每来源 IP 的上限、鉴权前的读取上限 4 KiB、口令使用常量时间比较
- 端口接管前复查状态；插件卸载期间不再接管端口；`shutdown` 等待轮询线程的上限提高到 20 秒
- 状态探测线程池改为常驻
- 假服务端 backlog 下限提高到不小于并发上限；接受成功后清除上一次的错误记录
- 写回 `config.json` 时保留文件中未知的字段
- 新增仓库根 `ruff.toml`（只启用 E9 与 F），并修正其报出的 18 处问题
- CI 新增三个作业：ruff 静态检查、最低支持的 MCDR（2.13.0）与压测 smoke、跨 MCDR 版本矩阵
- 打包产物不再提交进仓库
- 修复 `tools/soak.py`、`tools/mcdr_matrix.py`、`tools/check_artifact.py`；工具脚本输出使用 UTF-8
- 精简 README 与开发文档

### 验证

- 349 项测试通过
- MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 全部通过；2.12.0 被依赖检查拒绝
- 真机端到端场景 38/41 通过（未通过的 3 项为场景断言过时或场景噪声）
- 产物自检通过（可复现、内容白名单、注释与 docstring 已剥离）；`ruff check .` 与 `tools/soak.py --rounds 20` 通过
