# CHANGELOG

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本文件只保留最新一个版本的条目**，更早版本的记录见
[GitHub Releases](https://github.com/LifeSci-Craft/MCDR-IdleGuard/releases)。

---

## v1.2.3

### 修复

- **总线传来的音效与提示不再能当命令执行**：`sound` 与玩家名都按允许字符集校验，含换行等控制字符的消息被丢弃并记 warning。
- **对端探测不再拖垮整轮侦察**：对端探测单独用池、同一时刻只跑一次；状态查询的 15 秒总预算超时也不再丢掉整轮结果。
- `servers[].port` 增加范围校验，越界值只让该条目不可用，不再中断整轮侦察。
- 总线口令为空时不再监听，并在 `!!ig check` 中点名；数值与时间选项做范围校验（含 `inf`/`nan`）。
- `stats` 与「尝试连接 / 问候」容器在所有路径上都加锁；`startup_seconds.json` 原子写入，坏文件会被隔离。
- 外部启动也会结算休眠时长；`role=command` 的子服也会记录停服时间。
- `status_ping` 真正不再抛异常；接受循环在 `Thread.start` 失败后能自愈；假服务端的等关闭循环有总预算；未认证连接不能要求 2 MiB 缓冲。
- 心跳关闭（`state_tick <= 0`）时对端探测仍有独立节拍；端口接管重试有退避与上限。
- `velocity.toml` 不再静默丢弃单引号字符串与 IPv6 后端地址，并说明跳过了什么。
- 1.6 传统查询的声明长度对非 BMP 字符（emoji）不再少算 1。

### 工具与 CI

- 新增最小变异检查工具与 CI 作业；补 issue / PR 模板。
- 打包器会清掉包内 CHANGELOG 的旧版本节，并固定 ZIP 的 `create_system`，跨系统重建字节一致。
- CI 增加 `timeout-minutes` / `concurrency` 并固定安装版本。
- 元数据补上主页与许可证。

### 验证

- 测试 **542 项**全部通过
- 变异检查 **9 条全部被抓**
- MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 全部加载并通过 `!!ig help/status/reload`；2.12.0（低于最低版本）被依赖检查干净拒绝
- 产物自检通过（可复现、内容白名单、注释与 docstring 已剥离、包内 changelog 只留本节）
